"""Independent renderer: same LibreOffice binary/fonts/DPI for both outputs."""
import shutil
import tempfile
from pathlib import Path
from .renderer_process import run_renderer

def available():
    if not shutil.which('soffice'):return False
    try:import fitz
    except ImportError:return False
    return True

def _render(path,folder):
    import fitz
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='docxtpl-parity-lo-') as temporary:
        profile=Path(temporary).resolve().as_uri()
        result=run_renderer(['soffice','-env:UserInstallation='+profile,'--headless','--convert-to','pdf','--outdir',str(folder),str(Path(path).resolve())],timeout=120)
    pdf=folder/(Path(path).stem+'.pdf')
    if result.returncode or not pdf.exists():raise RuntimeError('LibreOffice conversion failed: '+result.stdout+result.stderr)
    fatal_prefixes = ('error:', 'fatal error:', 'libpng error:', 'libjpeg error:', 'libtiff error:')
    fatal = [line for line in result.stderr.splitlines() if line.lstrip().lower().startswith(fatal_prefixes)]
    if fatal:
        raise RuntimeError('LibreOffice reported an error: ' + '\n'.join(fatal))
    doc=fitz.open(pdf);pages=[]
    try:
        for i,page in enumerate(doc):
            pix=page.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
            pix.save(folder/f'page-{i+1:04d}.png')
            pages.append((pix.width,pix.height,bytes(pix.samples)))
    finally:doc.close()
    return pages

def compare_visual(reference,candidate,out):
    from PIL import Image,ImageChops
    out=Path(out);a=_render(reference,out/'reference');b=_render(candidate,out/'candidate')
    if not a or not b:
        raise RuntimeError('visual renderer produced zero pages')
    diffs=[]
    for i,(left,right) in enumerate(zip(a,b)):
        if left[:2]!=right[:2]:diffs.append({'page':i+1,'reason':'page_dimensions'});continue
        if left[2]!=right[2]:
            imga=Image.frombytes('RGB',left[:2],left[2]);imgb=Image.frombytes('RGB',right[:2],right[2])
            diff=ImageChops.difference(imga,imgb);diff.save(out/f'diff-{i+1:04d}.png')
            hist=diff.histogram();total=sum((i%256)*n for i,n in enumerate(hist))
            diffs.append({'page':i+1,'reason':'pixels','mean_absolute_channel_difference':total/(left[0]*left[1]*3)})
    return {'status':'compared','equal':len(a)==len(b) and not diffs,'reference_pages':len(a),'candidate_pages':len(b),
            'renderer':'LibreOffice -> PDF -> PyMuPDF, 108 DPI; exact pixels','differences':diffs}


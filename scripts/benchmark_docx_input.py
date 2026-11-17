import argparse, hashlib, importlib.util, io, json, os, platform, random, resource, statistics
import struct, subprocess, sys, tempfile, time, types, zipfile, zlib
from pathlib import Path

def chunk(kind, data):
    return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)

def fixture(path, size):
    from docx import Document
    document = Document()
    document.add_paragraph('input benchmark')
    if size:
        rng = random.Random(516)
        rows = b''.join(b'\0' + rng.randbytes(size * 3) for _ in range(size))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', size, size, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(rows, 1)) + chunk(b'IEND', b''))
        document.add_picture(io.BytesIO(png))
    document.save(path)

def worker(so, path, output):
    pkg=types.ModuleType('docxtpl_native'); pkg.__path__=[]
    sys.modules['docxtpl_native']=pkg
    spec=importlib.util.spec_from_file_location('docxtpl_native._native',so)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    data=Path(path).read_bytes()
    before=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    start=time.perf_counter()
    result=module.process_docx(data, '{}', False, False)
    elapsed=time.perf_counter()-start
    after=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    Path(output).write_bytes(result)
    print(json.dumps({'seconds':elapsed,'maxrss_kib':after,'increase_kib':after-before,
                      'input_bytes':len(data),'output_bytes':len(result)}))

def main():
    if len(sys.argv)>1 and sys.argv[1]=='--worker':
        worker(*sys.argv[2:]);return
    if len(sys.argv)>1 and sys.argv[1]=='--fixture':
        fixture(sys.argv[2],int(sys.argv[3]));return
    if platform.system() != 'Linux':
        raise SystemExit('Peak RSS units in this benchmark require Linux')
    parser=argparse.ArgumentParser()
    parser.add_argument('libs',nargs='*',help='compiled _native module paths; defaults to installed module')
    parser.add_argument('--out',help='optional JSON output path')
    parser.add_argument('--repeat',type=int,default=7)
    parser.add_argument('--image-size',type=int,default=1536,
                        help='PNG width and height; 0 tests a DOCX without media')
    args=parser.parse_args()
    if args.repeat<1 or not 0<=args.image_size<=4096:
        parser.error('repeat must be positive and image-size must be between 0 and 4096')
    libs=args.libs or [subprocess.check_output([sys.executable,'-c',
        'import docxtpl_native._native as m; print(m.__file__)'],text=True).strip()]
    with tempfile.TemporaryDirectory() as tmp:
        inp=str(Path(tmp)/'input.docx')
        subprocess.check_call([sys.executable,__file__,'--fixture',inp,str(args.image_size)])
        samples={lib:[] for lib in libs}
        for iteration in range(args.repeat+1):
            for lib in (libs if iteration%2==0 else reversed(libs)):
                out=str(Path(tmp)/(str(libs.index(lib))+'.docx'))
                raw=subprocess.check_output([sys.executable,__file__,'--worker',lib,inp,out],text=True)
                if iteration:
                    samples[lib].append(json.loads(raw))
        outputs=[]
        for i in range(len(libs)):
            with zipfile.ZipFile(Path(tmp)/(str(i)+'.docx')) as z:
                outputs.append({name:hashlib.sha256(z.read(name)).hexdigest() for name in z.namelist()})
        if not all(output==outputs[0] for output in outputs):
            raise AssertionError('candidate package contents differ')
        if args.image_size:
            with zipfile.ZipFile(inp) as original:
                image=hashlib.sha256(original.read('word/media/image1.png')).hexdigest()
            if any(output.get('word/media/image1.png') != image for output in outputs):
                raise AssertionError('image bytes changed in output')
        report={'python':sys.version,'platform':platform.platform(),'repeat':args.repeat,
                'warmup':1,'image_size':args.image_size,'results':[]}
        for lib, data in samples.items():
            report['results'].append({'lib':lib,'samples':data,'median_seconds':statistics.median(x['seconds'] for x in data),
                              'median_peak_rss_kib':statistics.median(x['maxrss_kib'] for x in data),
                              'median_increase_kib':statistics.median(x['increase_kib'] for x in data)})
        print(json.dumps(report,indent=2))
        if args.out:
            target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__': main()

"""Inspect a pair of generated ZIPs without relaxing binary comparisons."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import zipfile


def digest(data):return hashlib.sha256(data).hexdigest()


def compressed(blob, entry):
    start=entry.header_offset
    if blob[start:start+4]!=b'PK\x03\x04':raise ValueError('invalid local ZIP header')
    name,extra=struct.unpack_from('<HH',blob,start+26)
    start+=30+name+extra
    return blob[start:start+entry.compress_size]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--reference',required=True);parser.add_argument('--candidate',required=True)
    parser.add_argument('--candidate-revision',required=True)
    args=parser.parse_args()
    a=Path(args.reference).read_bytes();b=Path(args.candidate).read_bytes();parts=[]
    with zipfile.ZipFile(args.reference) as first,zipfile.ZipFile(args.candidate) as second:
        left=first.infolist();right=second.infolist()
        if [x.filename for x in left]!=[x.filename for x in right]:raise ValueError('ZIP entry order or names differ')
        for x,y in zip(left,right):
            xdata=first.read(x);ydata=second.read(y);xraw=compressed(a,x);yraw=compressed(b,y)
            parts.append({'name':x.filename,'content_equal':xdata==ydata,
                'reference_time':x.date_time,'candidate_time':y.date_time,'timestamp_equal':x.date_time==y.date_time,
                'reference_attributes':x.external_attr,'candidate_attributes':y.external_attr,
                'attributes_equal':x.external_attr==y.external_attr,'method_equal':x.compress_type==y.compress_type,
                'reference_compressed_bytes':len(xraw),'candidate_compressed_bytes':len(yraw),
                'compressed_bytes_equal':xraw==yraw,'reference_compressed_sha256':digest(xraw),
                'candidate_compressed_sha256':digest(yraw)})
    print(json.dumps({'source_revision':os.environ.get('GITHUB_SHA'),'candidate_revision':args.candidate_revision,
        'reference_sha256':digest(a),'candidate_sha256':digest(b),'parts':parts,
        'counts':{key:sum(part[key] for part in parts) for key in
            ('content_equal','timestamp_equal','attributes_equal','method_equal','compressed_bytes_equal')},
        'notice':'Diagnostic of generated files; strict nested-binary comparison is unchanged.'},indent=2))


if __name__=='__main__':main()

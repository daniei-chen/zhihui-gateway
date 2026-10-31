"""Reconstruct the exact compression successor; never install or start it."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
BUNDLE=ROOT/'ops/workbuddy-compression'
spec=importlib.util.spec_from_file_location('zh_ingress_preparation',ROOT/'tools/prepare_workbuddy_ingress.py')
core=importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def prepare(source,destination,git='git'):
    plan=json.loads(core.regular(BUNDLE/'rebuild-manifest.json'))
    patch=core.regular(BUNDLE/'compression.patch')
    if plan.get('schema')!=1 or hashlib.sha256(patch).hexdigest()!=plan['patch_sha256']:
        raise ValueError('compression patch identity differs')
    for key in ('source_files','candidate_files'):
        values=plan[key]
        if len({name.casefold()for name in values})!=len(values):raise ValueError('case-colliding source')
        for name,sha in values.items():
            core.relative_path(name)
            if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('invalid source digest')
    source,destination=Path(source).absolute(),Path(destination).absolute()
    if destination.exists() or destination.is_symlink():raise ValueError('new destination required')
    if source.resolve()in destination.resolve().parents:raise ValueError('candidate must be outside source')
    for parent in (destination.parent,*destination.parent.parents):core.safe_directory(parent)
    before=core.inventory(source)
    if before!=plan['source_files']:raise ValueError('exact prior ingress source required')
    destination.mkdir(mode=0o700)
    for name,sha in sorted(before.items()):
        raw=core.regular(source/core.relative_path(name))
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('source changed while copying')
        target=destination/core.relative_path(name);target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as output:
            output.write(raw)
    core.apply_patch(destination,patch,git)
    after=core.inventory(destination)
    if after!=plan['candidate_files'] or core.inventory(source)!=before:
        raise ValueError('source or candidate closure drift')
    return {'schema':1,'files':len(after),'tree_sha256':hashlib.sha256(json.dumps(after,sort_keys=True).encode()).hexdigest(),
        'source_unchanged':True,'production_modified':False,'build_test_sign_publish_required':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--git',default='git')
    args=parser.parse_args()
    print(json.dumps(prepare(args.source,args.destination,args.git),indent=2))

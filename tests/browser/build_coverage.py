"""Keep baseline candidates separate from actual per-snapshot browser evidence."""
import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    for name in ('keys','viewer','inventory','notfound'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    source=Path(__file__).resolve().parents[2]/'contracts/page-button-capability.csv'
    rows=list(csv.DictReader(source.open(encoding='utf-8-sig')))
    if len(rows)!=209 or len({r['row_id'] for r in rows})!=209:
        raise RuntimeError('Reconcile changed interaction contract before generating coverage')
    artifacts={name:json.loads(getattr(args,name).read_text(encoding='utf-8'))
               for name in ('keys','viewer','inventory','notfound')}
    for name in ('keys','viewer'):
        evidence=artifacts[name]
        if evidence.get('production_connected') is not False or evidence['status']!='passed':
            raise RuntimeError('Only passed disposable-fixture business evidence may be projected')
        if {c['platform'] for c in evidence['cases']}!={'workbuddy','qoder','trae'}:
            raise RuntimeError('Missing platform evidence')
    observed={c['group'] for c in artifacts['inventory']['cases'] if c['outcome']=='rendered_inventory'}
    observed.update(c['group'] for c in artifacts['notfound']['cases'] if c['outcome']=='rendered_inventory')
    # Names deliberately match only the specific actions actually executed.
    proofs={
        'PB0008':'UI username/password filled; successful authority authentication',
        'PB0011':'UI login button; actual POST /api/login',
        'PB0042':'UI create with name and platform; Manager persistent readback',
        'PB0047':'UI platform checkbox selections; Manager persistent readback',
        'PB0049':'UI disable and enable; independent persistent readback after each',
        'PB0050':'UI delete confirmation; independently absent from authority list',
    }
    output=[]
    for row in rows:
        item=dict(row)
        item.update(interaction_verification='not_executed',verified_roles='',verified_platforms='',
                    verified_snapshot='',runtime_evidence='',
                    page_inventory_observed=str(row['ui_id'] in observed).lower(),
                    current_candidate_presence='not_reconciled',remaining_checks='Execute the full interaction and verify UI/API/persistence or documented unsupported result')
        if row['row_id'] in proofs:
            item.update(interaction_verification='passed_in_recorded_scope',verified_roles='admin',
                        verified_platforms='workbuddy,qoder,trae',
                        verified_snapshot=artifacts['keys']['source_sha256'],runtime_evidence=str(args.keys),
                        remaining_checks=proofs[row['row_id']]+'; repeat on the final unified release candidate')
        if row['row_id']=='PB0043':
            item.update(interaction_verification='partial',verified_roles='admin',
                        verified_platforms='workbuddy,qoder,trae',
                        verified_snapshot=artifacts['keys']['source_sha256'],runtime_evidence=str(args.keys),
                        remaining_checks='Creation dialog and returned credential observed; listing has no plaintext. Explicitly assert displayed credential equality, close/reload behavior and clipboard handling')
        if row['row_id']=='PB0045':
            item['remaining_checks']='Name edit is verified in CRUD evidence; quota and expiry edits still require field-by-field readback and actual enforcement'
        if row['row_id'] in {'PB0047','PB0048'}:
            item['current_candidate_presence']='present_in_candidate_keys_page; baseline absence retained separately'
        if row['row_id']=='PB0196':
            item.update(interaction_verification='visible_control_only',runtime_evidence=str(args.notfound),
                        verified_snapshot=artifacts['notfound']['source_sha256'],
                        remaining_checks='404 code and visible return link verified; click/back navigation still pending')
        output.append(item)
    args.out.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(__file__,args.out/'verifier.py')
    shutil.copyfile(source,args.out/'baseline-page-button-capability.csv')
    with (args.out/'interaction-verification.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(output[0]));writer.writeheader();writer.writerows(output)
    counts={status:sum(r['interaction_verification']==status for r in output)
            for status in sorted({r['interaction_verification'] for r in output})}
    report={'schema':'zhihui-browser-coverage/v1','contract_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'contract_rows':len(rows),'counts':counts,'combined_release_acceptance':False,
            'reason':'Evidence belongs to different frozen snapshots and limited interaction scopes; this is a work ledger, not a release gate',
            'artifacts':{name:{'path':str(getattr(args,name)),'source_sha256':value['source_sha256'],
                               'artifact_sha256':hashlib.sha256(getattr(args,name).read_bytes()).hexdigest()}
                         for name,value in artifacts.items()}}
    (args.out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'rows':len(rows),'counts':counts,'combined_release_acceptance':False}))


if __name__=='__main__':main()

"""Offline best-effort ComfyUI GUI JSON -> API JSON candidate (NOT final approval).

Real compatibility requires a pinned ComfyUI environment's /object_info and a
non-paying API schema check. Never feed this candidate directly into a paid run
unless it has been independently validated against that environment.
"""
from __future__ import annotations
import argparse,json
from pathlib import Path
from h3_contract import patch_api_workflow


def convert(gui:dict)->dict:
    if not isinstance(gui.get('nodes'),list) or not isinstance(gui.get('links'),list):
        raise ValueError('GUI workflow must contain nodes and links')
    nodes={str(n['id']):n for n in gui['nodes']}
    links={int(e[0]):[str(e[1]),int(e[2])] for e in gui['links']}
    reachable=set()
    def trace(nid:str):
        if nid in reachable:return
        if nid not in nodes:raise ValueError('Missing referenced GUI node '+nid)
        reachable.add(nid)
        for inp in nodes[nid].get('inputs',[]):
            if inp.get('link') is not None:
                origin=links.get(int(inp['link']))
                if origin is None:raise ValueError('Missing GUI link '+str(inp['link']))
                trace(origin[0])
    roots=[str(n['id']) for n in gui['nodes'] if n.get('type')=='SaveVideo']
    if len(roots)!=1:raise ValueError('Exactly one SaveVideo output required')
    trace(roots[0]); api={}
    for nid in sorted(reachable,key=int):
        n=nodes[nid]
        named=n.get('widgets_values_named') or {}
        if not isinstance(named,dict):raise ValueError('Expected named widget values for '+nid)
        inputs={}
        for inp in n.get('inputs',[]):
            if inp.get('link') is not None:
                inputs[inp['name']]=links[int(inp['link'])]
            elif inp['name'] in named:
                inputs[inp['name']]=named[inp['name']]
        # API values for literal widget inputs; nodes may have non-input UI-only
        # widget keys. The real ComfyUI /object_info still determines validity.
        for key,value in named.items():
            if key not in inputs and key not in ('control_after_generate','upload'):
                inputs[key]=value
        api[nid]={'class_type':n['type'],'inputs':inputs,
                  '_meta':{'title':n.get('title') or n['type']}}
    # Semantic patch is required: no Turbo, locked poster/frames/steps/prompts.
    patch_api_workflow(api,'A')
    patch_api_workflow(api,'B')
    return api


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--gui',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    api=convert(json.loads(args.gui.read_text(encoding='utf-8')))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(api,indent=2,ensure_ascii=False),encoding='utf-8')
    print('CANDIDATE_ONLY',args.out,'node_count',len(api),'requires_live_object_info_validation')
if __name__=='__main__':main()

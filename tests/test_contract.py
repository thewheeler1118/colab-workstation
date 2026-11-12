import sys,unittest,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from h3_contract import *
ROOT=Path(__file__).resolve().parents[1]
class ContractTest(unittest.TestCase):
    def test_poster_hash(self):validate_poster(ROOT/'assets'/'original_poster.jpg')
    def test_A_requires_two_dodges(self):
        s=prompt_for('A');self.assertIn('SECOND TIME',s);self.assertIn('basketball',s)
    def test_B_contrast(self):
        s=prompt_for('B');self.assertIn('awkwardly',s);self.assertIn('recovers confidence',s)
    def test_frame_constraint(self):self.assertEqual(ANIMATION_FRAMES%17,5);self.assertEqual(TOTAL_FRAMES,274)
    def test_reject_old_ready(self):
        p=ROOT/'model_cache'/'verified_manifest.example.json'
        with self.assertRaises(ValueError):validate_model_cache_manifest(json.loads(p.read_text()))
    def test_reject_UI_workflow(self):
        with self.assertRaisesRegex(ValueError,'API format'):patch_api_workflow({'nodes':[]},'A')
    def test_reject_unknown_API_graph(self):
        with self.assertRaisesRegex(ValueError,'contract mismatch'):patch_api_workflow({'136':{'class_type':'Foo','inputs':{}}},'A')
    def test_good_manifest_shape(self):
        files=[{'path':path,'drive_file_id':'abc','size':100,'sha256':'a'*64,'server_verified':True,'hf_revision':'fixed_commit'} for path in MODEL_FILES]
        validate_model_cache_manifest({'status':'READY','verification_method':'drive_v3_server_api','account_verified':True,'verified_at_utc':'2026-11-10T01:00:00Z','files':files})
    def test_api_patch(self):
        names={'136':'MiniMaxH3ReferenceToVideo','137':'LoadImage','139':'LoadImage','124':'BasicScheduler','129':'RandomNoise','92':'SaveVideo','127':'UNETLoader','128':'CLIPLoader','146':'PrimitiveBoolean'}
        j={k:{'class_type':v,'inputs':{}} for k,v in names.items()}
        p=patch_api_workflow(j,'A');self.assertEqual(p['136']['inputs']['length'],226);self.assertEqual(p['124']['inputs']['steps'],25)
        self.assertEqual(p['137']['inputs']['image'],'poster.jpg')
        self.assertEqual(p['127']['inputs']['unet_name'],'minimax_h3_ref2va_pruned_int8_convrot.safetensors')

    def test_api_patch_strips_dead_turbo_lora_branch(self):
        # 2026-11-12: the Turbo LoRA file is not in the 4-file depot and
        # the switch is forced off, but the server validates the whole
        # reachable graph, so the dead branch's missing lora_name 400s
        # /prompt. The patch must remove the loader and point both switch
        # branches at the plain model.
        names={'136':'MiniMaxH3ReferenceToVideo','137':'LoadImage','139':'LoadImage','124':'BasicScheduler','129':'RandomNoise','92':'SaveVideo','127':'UNETLoader','128':'CLIPLoader','146':'PrimitiveBoolean'}
        j={k:{'class_type':v,'inputs':{}} for k,v in names.items()}
        j['145']={'class_type':'LoraLoaderModelOnly','inputs':{'model':['127',0],'lora_name':'minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors','strength_model':1}}
        j['141']={'class_type':'ComfySwitchNode','inputs':{'switch':['146',0],'on_false':['127',0],'on_true':['145',0]}}
        j['126']={'class_type':'BasicGuider','inputs':{'model':['141',0],'conditioning':['136',0]}}
        p=patch_api_workflow(j,'A')
        self.assertNotIn('145',p)
        self.assertEqual(p['141']['inputs']['on_true'],['127',0])
        self.assertEqual(p['141']['inputs']['on_false'],['127',0])
        self.assertEqual(p['146']['inputs']['value'],False)
        self.assertNotIn('lora_name',json.dumps(p))

    def test_api_patch_real_workflow_has_no_lora_reference(self):
        # The exact submitted workflow must not reference the Turbo LoRA.
        api=json.loads((ROOT/'workflow_api.json').read_text())
        p=patch_api_workflow(api,'A')
        self.assertNotIn('145',p)
        self.assertNotIn('lora_name',json.dumps(p))
        self.assertEqual(p['141']['inputs']['on_true'],['127',0])
if __name__=='__main__':unittest.main()

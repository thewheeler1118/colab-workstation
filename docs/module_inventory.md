# Module inventory

Every migrated file: relative path, size, SHA-256, type, role in the production flow,
last known real run result, whether the offline suite exercises it, and secret risk.
Hashes were computed from the files on disk at migration time and refreshed
2026-11-12 during TASK-002 (sizes/hashes re-derived from disk; new TASK-002
files appended); re-verify with `python3 scripts/verify_manifest.py`
(which also covers `SOURCE_MANIFEST.json` itself).

Note: `SOURCE_MANIFEST.json` and this inventory file itself are not
listed in the table below — the manifest's hash cannot be listed
inside itself, and this file's hash is recorded in
`SOURCE_MANIFEST.json` (computed after this file was written).
Both are covered by `scripts/verify_manifest.py`.

| File | Size (bytes) | SHA-256 | Type | Role | Last known real run result | Tested | Secret risk |
|---|---|---|---|---|---|---|---|
| `.github/ISSUE_TEMPLATE/bug_report.md` | 446 | 73d56488295e5237e990ee7de42d102e49ed04b355838ae3c31860e5219008be | Markdown | CI / templates | n/a | n/a | Low |
| `.github/ISSUE_TEMPLATE/feature_request.md` | 473 | daf222d23898bdb2eb530c7e68311eede32db1e254313504687d5908dacc84d8 | Markdown | CI / templates | n/a | n/a | Low |
| `.github/pull_request_template.md` | 933 | af891f5962b97fab7bc7d49f6b8b1287841ec56e40058413da6513de35559b72 | Markdown | CI / templates | n/a | n/a | Low |
| `.github/workflows/ci.yml` | 3889 | 062c68c75e11e4e4c34b3daaac6d8e9e17d8c5a1d9bbb35581a9006bb820a518 | YAML | CI / templates | n/a | n/a | Low |
| `.gitignore` | 646 | 1c97685e9ba6e35d8bb2555b7d6928c6857d718f6ddea214429a36901d190f79 | File | Supporting file | n/a | n/a | Low |
| `AGENTS.md` | 3989 | 63ddf91e9d3a2066a35f54558fd510593588093829507ba0f4b7cdde20b0613d | Markdown | Supporting file | n/a | n/a | Low |
| `CHANGELOG.md` | 6269 | e777e80c1b3241600cbf70e6612e470d61b9b23d95306b1b460d44a8533ef7a8 | Markdown | Supporting file | n/a | n/a | Low |
| `README.md` | 7804 | 89d049361a6f1b4e2d5c26ab79d5b5d5337dcf9ce669d25429b296ae233a2d2b | Markdown | Supporting file | n/a | n/a | Low |
| `assemble.py` | 2927 | cca6a5feb2c49636373bc977343b21b52b669934df724d0de5d86aedf6ea30fe | Python | Video assembly: 226 generated frames + 48 poster frames = 274 (ffmpeg) | A run h3-20261112T090027Z-8b07c6: assembled 274f, A.mp4 7,453,808 B | N | Low |
| `auth_support.py` | 1729 | 20856f74b1c6278f90def9992f2d3e0de68ab36d61f28344fc0c0a4a57e26c20 | Python | Colab CLI resolution + ADC credential-metadata guards (never reads secrets) | Preflight ADC/CLI checks each run; A/B runs 2026-11-12 | Y | Low — metadata only, never reads OAuth secrets |
| `build_api_candidate.py` | 2974 | 69b7344ec405746023c84a648fa59186b10a95871f14e6cc5cd9dabe1e5674b0 | Python | UI-format workflow -> API-format candidate (validation only) | API candidate generated 2026-11-10 (26 nodes, static contract OK); not a live acceptance | Y | Low |
| `config/budget_policy.yaml` | 1147 | 0859bd9b57628b34b5a4980663b5bec1c8ceebb72fb76131ee2ebeedc148e8a3 | YAML | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low |
| `config/comfyui_source_manifest.example.json` | 1441 | bbdaf9d30e05f7306cfb1d9ac50c07f7e2afeb08b9194bfab5f0c663b0886baf | JSON | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low — Drive file IDs + hashes only |
| `config/depot_config.example.json` | 339 | 84c5d4725645588c5828f4b0c74e44a8cf23a96f30764bfa8859088400aaaea6 | JSON | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low — Drive folder/file IDs only |
| `config/gpu_policy.yaml` | 1043 | fc17612525557c6c8f7d7a7e331af036b7ec67669b722710eef11c5d05e3cdd1 | YAML | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low |
| `config/model_manifest.example.json` | 2517 | 51ad1aee57248bbcced179626281f16c406d79ebbd60d1e3e88cbc77fa7aece5 | JSON | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low — Drive file IDs only (non-credential references) |
| `config/pinned_hf_files.json` | 8592 | b1b0c868ec9bcfbc295247724f23f88be26aa42e870f96a8a0fcd109ed1745d3 | JSON | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low |
| `config/video_profiles.yaml` | 789 | 8d3a999650590314f368c5b179bcfb2165154a79527a5fb6b81f4e9859de4ec4 | YAML | Configuration (mirrors code constants; asserted by tests) | n/a | n/a | Low |
| `docs/architecture.md` | 4855 | ef4fa43feee537c99e1abcee6b497bf846dd568b4b4941cdcd4163295f9047f0 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/audits/README.md` | 773 | 9f0c388ef229ede23e3a72d8d6f1326e02dae0b617f84f6aece8ce1f9e4b3e70 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/audits/archive_p0_code_level_prompt.md` | 12153 | 10ed3f6bb4f2babed41a8d78cc1428e0aaf379635e9716e51520c0600c492de4 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/audits/archive_p0_readme.md` | 11553 | efd93b5e038215b4ad2c9d10d9dae71749484c4cf386074d08bdd53f76adb9a5 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/audits/v3_cli_proxy_patch.md` | 2342 | 8c7fcb614c19459d784a4e6e35be85ce6977428aa5f689e69d1f3d70f1877df7 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/audits/v3_drivemount_timeout_patch.md` | 5674 | 2c1e5f4da9b40da183cf3a6d81a9b550f6dca55aec9306951ceef5bee44d25bf | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/code_provenance.md` | 4354 | 26ae038be03cc355a0d31096b5b5927cc88b8994427908950629eea57279583d | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/decisions/ADR-001.md` | 946 | f23d1e16b52e19e2267de7405ee95f8fe7a38186811e3e6cdb1ca3e8bfc2467d | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/decisions/ADR-002.md` | 756 | 003989f7387a3c3e3ca1dda822b88129874e8d8c8457b76598a2c9c42c3424b1 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/decisions/ADR-003.md` | 762 | 4a4cde32f47c1e555b83db895377dbc4c094d04e8290e681b31cec542d20b197 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/decisions/ADR-004.md` | 699 | ada836446059a205636299ff0dd6a18088db6910358aaf21e7a880808f61a0f1 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/decisions/ADR-005.md` | 970 | fe15ad8adaf84c49865ec29d37efeb8031bad1121bca1b03040d8937984eabf9 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/deployment_status.json` | 4183 | c0ac99a84455aa099c948a96401e8a316eddba6d893c743352d50a30b255b562 | JSON | Documentation / evidence | n/a | n/a | Low |
| `docs/execution_flow.md` | 10092 | 5476273516421576a8fb15748d8e38dabe1a41aa444d5f17f109180e1bbdb934 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/01-proxy-411-assign.md` | 811 | 09617c87bc1097f20545627d9ffd5fcdcfca8ceaad4899e8649b6dcf33edc9d9 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/02-empty-post-body-patch.md` | 723 | 51443a09e60fb78037130ced1df754d059f160cc3e6946b0fca3c6a555bcd900 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/03-gpu-503-g4-to-a100-fallback.md` | 752 | 1b5100fd2de2083c1c51cb80d0b5db56b57e986769552f7659d990e8154878c2 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/04-a100-40gb-misallocation-risk.md` | 730 | d1e1a524121d95c10b3c581cf8bcf8206f43238fe18ed8921a651869f69c4ab5 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/05-gpu-probe-readtimeout.md` | 681 | 7de988ac9c035e51f5b962c0393d70b20b423a417d74fcbc5c84ca54d48e73a6 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/06-drivemount-120s-default.md` | 865 | 6599e3e79367769d4395a43874b340fdb261db3e0cba891aea7a337264b8ba47 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/07-drivemount-timeout-ms-600000.md` | 1005 | 626a14cb23b756120fb3202b1537025e481c1d684c1cdf1c8d686fb00f9b8fa4 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/08-browser-oauth-pty-credential-propagation.md` | 943 | 449c5b2e0fba224a62adafdeba4f3732db109a0a4f825accf3432a1921990bf0 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/09-mount-exit-zero-remote-failure.md` | 974 | e27c08f6fa6124de3332969604702743175210b40de5769ab8af370e0477b037 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/10-drivefs-four-weights-visible.md` | 683 | f25ed882d13f8c0f9a42978c30744e7405f044c6fff825ab7e5f421637d9853e | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/11-drive-40gb-restore-speed-disk-gate.md` | 853 | cce4747b6232ab0a7c52720651a17fb72f5c0a5a132c1af61e90727858625af6 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/12-vm-restart-orphan-gpu.md` | 1301 | 9044bfae92a4a31bad8f4df38def414f6081b6d9dd1f7c8ed42ef3c853520662 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/13-cli-update-notice-balance-misread.md` | 880 | 98a2038637e55890144dbcb983d9bd1ba8f8ccfb00eabdc5b8e2b38ecd7aade8 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/14-cu-billing-delay-unbilled-estimate.md` | 966 | e3b08dd70e843e5659f2832b0581ef80bbe90cdc58e96c769a7b4607627a5150 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/15-git-clone-exit-128-stderr-lost.md` | 959 | 9b92e44f3a0e34bf4e2e9b80436b24eda4b5a083ce479f0e729609d597ce31b6 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/16-comfyui-pinned-install-source-cache.md` | 874 | 467279b84e0c94d094785eda14fb6f8f46137041ba7cae961d7000746ec8f8e3 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/17-worker-early-exit-controller-misjudged-success.md` | 900 | 84ecaa8d0aa8f9cb6b47424fdcb9f9634adbd93aabc4c4a607eb72a27190b825 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/18-worker-stdout-stderr-not-persisted.md` | 758 | f3a9ed72c8521cd733d53b7308f7116563ef549e25b2ffecd053b48fb52a9705 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/19-comfyui-prompt-400-turbo-lora.md` | 792 | a5b93a57b84c8496c025128af8932f3b321438deac0532a7060ab7ab2180e4f9 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/20-comfyui-v3-input-key-compat.md` | 659 | 448fc6e44e56523a013d66c0991d997b8288b41f1939e2ae8f09dd80e71c822c | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/21-minimax-h3-25-steps-success.md` | 662 | 5022695a0404f5ba4153568395601e97e9498c829567ce62f85f2c9d2f3f336d | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/22-savevideo-history-schema.md` | 1026 | 6786a8e2cb614fe9e2fbf671ad54e2dfaf217fb8fae5ddb027c8c3a292923ab9 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/23-mp4-generated-not-downloaded-lost.md` | 941 | 466402ae53cfa2e90ab48b24a3321765008d854f5e9060662dc80c6fb08ad4ed | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/incidents/24-drive-output-not-server-verified.md` | 866 | 67dbc2852f9fe38e0500d09f5c1829665d5332b544c8aab61b1babfd3b94e4ca | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p0-persist-mp4-before-shutdown.md` | 1181 | 1c41e34869bc4de8e352c42d0951a14343b52e380a0d7ee75f6c9a2d76a6bfa8 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p0-savevideo-history-schema.md` | 1422 | 9004bc8a13bcd3bbc1961da1a1c1415f6c8063bec95202daf3ccfd9d27bf9298 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p1-account-safe-30cu-ledger.md` | 1016 | ae96a513671e17a937b4a9724fe1303dae07aecb796b2c395c5b460306dd7cbb | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p1-comfyui-api-workflow-contract-tests.md` | 1047 | a42f04df519395db5b3fb5c3ed951e6d87fa53db263e14c8f581c0ec6b366a56 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p1-continuous-worker-progress.md` | 1007 | c38b4475cf5355aefce7b56e60773df3dcbd2cf0131f18234c335543fc40f30e | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p1-reproducible-comfyui-bootstrap.md` | 1080 | f65c81fe50211cf1d5e1c3160452e9da9ed74a0bd9604f8b7b189331a40f7583 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p2-drive-api-download-benchmark.md` | 1005 | 58eb359eb2d2d790ff9d5cedcc48179ed5616f1289c61c00715b2fe7085a27cf | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p2-evaluate-minimax-h3-clean.md` | 1169 | 6e2f34cafd680830a793247123081206fbb82f0ffb15d0cc86506983a6c60b4b | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/issues/p2-g4-a100-cost-latency.md` | 848 | 3492d1b9503cd9551de93b6e5223bb82cb920228fdfffdd378fda1003f0e9106 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/muse_code_level_prompt.md` | 15326 | 8d42e0c573c18568425d3588388a15c4687a759f39cc58af16141a7ac7a3c5a9 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/muse_execute_auth_depot_video.md` | 15326 | 8d42e0c573c18568425d3588388a15c4687a759f39cc58af16141a7ac7a3c5a9 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/research/colab-ultra-workstation.md` | 490 | 38b9967627afc5ff73bf63d829ec14f62dfa64798f21b2b1aaccf1184cde71fb | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/research/drive_v3_remote_download_design.md` | 40588 | 25f83fd03d3cd43613264f9528503e400c96b0f5b0334f60acac53fe40e132ce | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/research/google-colab-cli.md` | 628 | d579882c1958ce89b09a213e0b03b69cd07f765f21bc5fa3430005489655d574 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/runbooks/preflight.md` | 1675 | 10ebbe53c21b93e67281e0f00500f41c7129a4fe4020fa638c303b56e5209080 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/runbooks/production-run.md` | 1543 | 382bbd3aab3f9009dfa96c6807671762600584d04795838e15955d81fc5dc588 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/runbooks/shutdown-verification.md` | 1003 | 68a27a4e67a5e5160ad94b3f498919807c482e50fccb84b35c81fbaf9ca23be0 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/runbooks/vm-restart-orphan-recovery.md` | 1628 | dfd909e6a8cab46ed65f02e5e8e6dd473716f8334f32227a05d3724bd9f1e7ff | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/storyboard.md` | 1332 | 8536845ec69e4ca12d1139abc5a4251ec233db9f87eba0c1b5553c544161ac6f | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/storyboard_full_drive.md` | 3305 | 5930c71a3430deaef6cc9e15caabc700bc70c479cb8e447fa6921b677c8bbbc2 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/v3_progress.md` | 11973 | 097248cca709b1843a44fa944e13f2516d61d02be8c97509ea9d139580d9180a | Markdown | Documentation / evidence | n/a | n/a | Low |
| `docs/v3_readme.md` | 3636 | 7a8ca3b8584945cf6e68c0b4d4663d133162c0aeb91a086f3482aa2efc7da6a1 | Markdown | Documentation / evidence | n/a | n/a | Low |
| `drive_api.py` | 3329 | 5c3dc379e156fdc81c42b86d4bc9f119b51d4db80750e769432968bd2c30d545 | Python | Drive v3 file evidence + upload helpers | Depot seed/verify 2026-11-10; used by archive path (not yet live-invoked) | N | Low — handles tokens in memory, never logs them |
| `drive_depot.py` | 30132 | 20b7eb9d105c3585accdd646ff01b860e4d365251ec9922dac349c48fc6fb8d5 | Python | Model depot init / seed / verify via Drive API (READY manifest) | Depot CACHE_READY 2026-11-10T22:36Z (4 files, 40,073,842,159 B); re-verified per run | Y | Low — handles tokens in memory, never logs them |
| `drive_resumable.py` | 8538 | 22dced6d58edfff8293601e82730acd80cad9e4f3b0190be08afdd4cd57bbdb9 | Python | Resumable upload with 308/Range resume + empty-PUT 411 fallback | 128 MiB probe uploads verified 2026-11-10 (incl. mid-upload kill + resume) | Y | Low — handles upload sessions, never logs secrets |
| `examples/run_manifest.example.json` | 339 | 14ac868efeb3c4bd02370ab6e3619b18371466435ad23effe9b15a359824b5bf | JSON | Example (placeholder values only) | n/a | n/a | Low |
| `examples/sanitized_worker_result.json` | 524 | c4bd9649b5f8ee3a01f1391812f266d62ffbec3abf061451b4e6a6a077674854 | JSON | Example (placeholder values only) | n/a | n/a | Low |
| `h3_budget.py` | 21236 | 7646d3a7404c79b3a12bb2d7c4523f8be9ec360387f4d965509a0557020de184 | Python | Per-task CU ledger (30 CU) + GPU choice policy (G4/G4/A100_HIGH_MEM) | Ledger h3-ab-20261111: baseline 162.77, max observed spent 18.58 (2026-11-12T09:49 UTC) | Y | Low |
| `h3_comfyui_cache.py` | 12381 | aaea4e824b1c882879d05104b3e079eb2afc71a5275f8fb91810fa21f5334356 | Python | ComfyUI source archive build / verify / publish to Drive cache | A run h3-20261112T090027Z-8b07c6: source cache restore OK | Y | Low |
| `h3_contract.py` | 8865 | 20ad3c380bfdeae0f32ba5ef1fa15b1227603d9f3eebfc84d8d69cae304de369 | Python | Video contract, A/B prompts, API workflow patch (pinned nodes) | A run h3-20261112T090027Z-8b07c6: COMFY_OBJECT_INFO_VALIDATED, /prompt no longer 400 | Y | Low |
| `h3_controller.py` | 64428 | 2bc7792b3066495db1c1b7a46882fb7b6debbbfa0f4220bb97fbe342ce20da38 | Python | Run orchestration: preflight, allocation, mount, worker exec, download, stop | A run h3-20261112T090027Z-8b07c6 SUCCEEDED; B run h3-20261112T093207Z-7ab515 interrupted by VM restart | Y | Low |
| `h3_drive_probe.py` | 1109 | 1b2b2ff4175073011a42a51bde8188fdf7ec9d5fc73b67750d1f40ecc956ef8a | Python | Remote mounted-model visibility probe (4 files, exact sizes) | A run h3-20261112T090027Z-8b07c6: MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4 | N | Low |
| `h3_gpu_probe.py` | 1541 | b7c02f61f9127a68d72ad21de32d68c633d5b61c19990ae239a0c31f5a48e9df | Python | Remote GPU hardware probe (nvidia-smi, name tokens + min VRAM) | B run h3-20261112T093207Z-7ab515: A100 High-Mem 80GB probed OK | N | Low |
| `h3_init_remote.py` | 189 | 268c184c83c7551065938b661fc0412e871d3143d6e8c99bd7af367e5d6350c2 | Python | Remote workdir init script (run on Colab via colab exec) | Executed remotely in A/B runs 2026-11-12 | N | Low |
| `h3_mount_acceptance.py` | 15607 | 3c621c48f9ab2007662a52135479fc70b3591bb0e23465e02675f634cf993f7e | Python | Mount acceptance checks (local workflow + manifest validation) | Preflight acceptance each run; A/B runs 2026-11-12 | N | Low |
| `h3_mount_signature_probe.py` | 564 | 67bb9b0510e2fd32e82643d640943f0cf8992c329f321db45a562bd08c1ee113 | Python | Remote drive.mount timeout_ms signature probe | A/B runs 2026-11-12: signature accepted, mounts proceeded | N | Low |
| `h3_mount_timeline.py` | 9457 | b261fcdd5cdd8601f3ad64c9ea0875c3d6bdc8dadcaaa2d9dabe4f7b4cdce720 | Python | Mount event timeline from stdout markers / history events | Mount timelines built for A/B runs 2026-11-12 (95.4s / 70.7s) | Y | Low |
| `h3_output_check.py` | 2661 | 3b7fe975685248786e239efac15d692d47543b68e6666eb0f5d42a84e3e40922 | Python | Remote final-MP4 ffprobe check (OUTPUT_CHECK_JSON, 274f/24fps/1080x1920) | A run h3-20261112T090027Z-8b07c6: OUTPUT_CHECK_JSON ok=true (274f/24fps/1080x1920) | N | Low |
| `h3_remote_worker.py` | 40886 | 5739f060cca64ded6dd982b78e3af727129d9420833ff2a6f2d1a1b1e47f544f | Python | Remote ComfyUI worker: restore, inference, find_rendered_video, assemble | A run h3-20261112T090027Z-8b07c6 SUCCEEDED (MUSE_H3_DONE_JSON); B run interrupted pre-inference | Y | Low |
| `h3_restore_probe.py` | 1815 | 905a9d4d2e2b0a424e8b671ae45eff1651bab38a280681effb608be8ac6df8eb | Python | Restore probe output parsing | Offline tests only; no standalone live run id on file | N | Low |
| `h3_session_watchdog.py` | 13875 | df3f2b7bd869220b25b1ae0a888d268a766948ba41ba5da1d5096de60e5308b4 | Python | Managed-session registry, deadline watchdog, stop + reconcile | Orphan stops 2026-11-12 (runs 061a20, 6d89ae, 7ab515); 0 active sessions verified | Y | Low |
| `h3_video_qa.py` | 6370 | 79bab6d77c37e837c066ca762684d479e20867564e7790be44834510241236be | Python | Local final-video validation + poster-tail PSNR QA | A run h3-20261112T090027Z-8b07c6: QA passed, poster-tail PSNR 31.38 dB | Y | Low |
| `jobs/minimax_h3_ab/README.md` | 2257 | 234a60f51a8511db90879d761edb4bc34ccfd6ed4ba080981b19d0da02edbd00 | Markdown | Job definition / facade | n/a | n/a | Low |
| `jobs/minimax_h3_ab/job.yaml` | 1625 | d10e2c3a31d5fe23880ead215fd1c1cbd23eba118173edd3f3bad39cb0fd3b1a | YAML | Job definition / facade | n/a | n/a | Low |
| `jobs/minimax_h3_ab/video_contract.py` | 1171 | b149891e645453a0a9eaab319edfe4cbcc7520c46503798ec24ebc768e498ca1 | Python | Job definition / facade | n/a | N | Low |
| `model_cache/verified_manifest.example.json` | 798 | 99c888f61ab1987d429f3f04d76baa22641a71e6485c30e4658ac7013287e712 | JSON | Sanitized manifest example (nulls); used by tests/test_contract.py | n/a | n/a | Low |
| `patches/google-colab-cli-0.7.5/README.md` | 2435 | 6fbfba57d0eb8531440e03660571719a1f33074b07155266b31d69938d84b376 | Markdown | Vendored CLI patch | n/a | n/a | Low |
| `patches/google-colab-cli-0.7.5/drivemount_timeout.patch` | 1700 | fc7cbde9363c71f0aeaeed941376208f6e14ae6cf85e636c1813f0ea356ae9f9 | Unified diff | Vendored CLI patch | n/a | n/a | Low |
| `patches/google-colab-cli-0.7.5/proxy_post_body.patch` | 811 | 0f5c5d9b73b90dca4d928261f28af1199138785a186f1fe9de1bc7aa9a3be639 | Unified diff | Vendored CLI patch | n/a | n/a | Low |
| `pyproject.toml` | 870 | eec8ae41c9cdf5330c6b90f4fbdeb9c9af268c7ba467ec1b5202147f410149b4 | TOML | Supporting file | n/a | n/a | Low |
| `reference_v2/README.md` | 1956 | 1f6168b2eef466739de09c458d55a3aed15e91b91a03a6d0094a56e67f106b2a | Markdown | Supporting file | n/a | n/a | Low |
| `reference_v2/colab_manager.py` | 145688 | c7f7a9103beff8c5d974bfb2b99f8273787490c7e7ed533555d318e075710300 | Python | Vendored CLI output parsing (usage/sessions) + log redaction | Parser functions used by controller inspect() every run | Y | Low — contains redaction regexes (patterns, not values); allow-listed in CI |
| `reference_v2/generate_video.py` | 33988 | 47a3d3eb288b4ba77e7eb1917e874c5cc81903207edc80952a026d2e5b2c1e45 | Python | Vendored video parameter validation (imported by colab_manager) | Imported by colab_manager; validation helpers used by its parsers | N | Low |
| `requirements-local.txt` | 195 | 8f05cddeea346a93d61727fcdffe8e759b6bc1d116d50c99b0cfe7b54965e57b | Text | Direct dependency floors (local/CPU env only) | n/a | n/a | Low |
| `requirements.lock` | 1892 | 74ee87b19761add0f26b2bd57160809599023d94a6f66fff118eb3c932c40494 | Lockfile | Supporting file | n/a | n/a | Low |
| `scripts/check_budget.sh` | 838 | b9d8235b6d9250bc847650c6dfb69026ead7068f498a977c558c1f73061b4902 | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `scripts/check_docs_index.py` | 1506 | 70f21b7687bb61e8f3071ccabcbaf97847e41ad07d7169646324f8c2b0521b4a | Python | Operator wrapper / offline check | n/a | N | Low |
| `scripts/check_outputs.sh` | 691 | 44f664f86b6f770a474d8da16545b758bdfc4e64b55d4e98c9ffcd1deabda5a1 | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `scripts/check_sessions.sh` | 727 | c0172ad45a39efa765a3137f9a4794218a59597253ee152e49c9498d0aa93c96 | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `scripts/check_workflow_schema.py` | 2555 | 357a384b79976039a5f394e75a920dfe68a35bc799585980fef91c341733a986 | Python | Operator wrapper / offline check | n/a | N | Low |
| `scripts/preflight.sh` | 553 | 1c6050513e4877c44232010359f73845299f886238f8c9a8dbbf0e56990f4aa0 | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `scripts/run_h3_ab.sh` | 2500 | f3573db85b685c59fc59c4f684c1c10fcf717b68bb4ffaf1f4d5d267dee21da3 | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `scripts/verify_drive.sh` | 1369 | 5adead763e93c0941c5c2036c89eecd2c0aa0e3729b9e4cb9a486f8587cf564e | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `scripts/verify_manifest.py` | 2260 | c8b3cdafcf4c31855374163993406bcc7f8ad52d10b3c79c1632d6262990a213 | Python | Operator wrapper / offline check | n/a | N | Low |
| `scripts/verify_patch.py` | 3130 | a2ee14fdeabf5105b4f2655886f99ce07333bc761f3fed5098cfa9c6f7d184c9 | Python | Operator wrapper / offline check | n/a | N | Low |
| `scripts/verify_shutdown.sh` | 1155 | f4e6e45b2c3494ccd9731a63850fe8e3a335b02d9732b0bfd5df0f2a6eada065 | Shell | Operator wrapper / offline check | n/a | n/a | Low |
| `tests/live_drive_resume_check.py` | 6300 | db18f4f4c04db07935f74e874971721120c0d56a1d7a273a07aea11a80217583 | Python | LIVE-ONLY check (touches real Drive; excluded from unittest discovery) | n/a | self | Low |
| `tests/run_ci_tests.py` | 2189 | 2c0046b1a9f83301b5a2dac154bcc73f60f2805132113b0ff24e014380a9e1f9 | Python | CI entry point: discover minus 2 asset-gated poster tests (printed skips) | n/a | self | Low |
| `tests/test_auth_and_transport.py` | 4953 | d7c565e83574fa377381006b9f3e5ab2adb053c80708c5ec1a051ca0476d537d | Python | Tests: CLI lookup, ADC metadata, Drive mount gates, READY guards | n/a | self | Low |
| `tests/test_config_contract.py` | 3257 | c9f0741a3147d7bfd04a7a7b052856859e81cf86ab71a3ebe3d6bf042ad0e8d5 | Python | NEW tests: config YAMLs equal code constants (4 tests) | n/a | self | Low |
| `tests/test_contract.py` | 3995 | 76710817aa23fe5c2f5634334ca80bf4515e8bb823e49ba6a82928a58884bf53 | Python | Tests: video contract, prompts, workflow patch, poster hash (asset-gated) | n/a | self | Low |
| `tests/test_direct_download.py` | 10092 | 979b7c601ba9a78db4fc8e0f9e5774a4f932d39963ca9fd2d32c18172fcb19e0 | Python | Tests: direct Drive download path (mocked) | n/a | self | Low |
| `tests/test_drive_depot.py` | 10782 | 821da750e96c3ace28f8afefb87bc1755262fa8ea6b2d667308fbf102146c13c | Python | Tests: depot verify / manifest guards (mocked) | n/a | self | Low |
| `tests/test_drive_resumable.py` | 5725 | 2eca69d133a10733133e17f36b35f95771c27597df9d556a1376300ffa64a964 | Python | Tests: resumable upload, 411 fallback, cross-process resume | n/a | self | Low |
| `tests/test_h3_comfyui_cache.py` | 16603 | 40d9bae8d6b58e9ee1ce74cb1520ec831158f7df204dee8ce7ba85ac5b4298e1 | Python | Tests: ComfyUI cache build/verify/publish (21 tests) | n/a | self | Low |
| `tests/test_h3_mount_repair.py` | 24057 | 8050d2977411b0bfe8b5646f96e71991e30c3d0e78ae6f3198b61f5931b31ab8 | Python | Tests: mount repair, CLI patches, budget/watchdog/restore gates (31 tests) | n/a | self | Low |
| `tests/test_h3_production.py` | 42074 | 6f21e909f06979c97c37a91f5d2a8f6a8003c5e72d7b2186728b90220fe90bd1 | Python | Tests: production flow incl. worker marker, stop, parsers (63 tests) | n/a | self | Low |
| `tests/test_savevideo_history.py` | 12679 | 0b680e5c3985676fc5215cabf16784a5bdf6e7dce1125463ac58e21fefa23b30 | Python | NEW regression tests: find_rendered_video history shapes (8 tests) | n/a | self | Low |
| `workflow_api.json` | 7562 | 7f6dab9f1eff0a9e73808e5cefb909be4cc0099816f3db05ade5245a9bf993f3 | JSON | Canonical API-format MiniMax-H3 workflow (submitted to /prompt) | Submitted in A run h3-20261112T090027Z-8b07c6 after patch_api_workflow | n/a | Low |
| `workflows/README.md` | 2332 | 89fe8a0627b4fc87dbdf279becfdda0a2ec99fd53f8656dbffab968a24c89f1f | Markdown | Workflow artifact | n/a | n/a | Low |
| `workflows/api/workflow_api.json` | 7562 | 7f6dab9f1eff0a9e73808e5cefb909be4cc0099816f3db05ade5245a9bf993f3 | JSON | Workflow artifact | n/a | n/a | Low |
| `workflows/schemas/api_workflow.schema.json` | 2155 | bcabd932609aabeaa4e88c149e6d67cdf90f778d6cf0768de77fc97f8bd06989 | JSON | Workflow artifact | n/a | n/a | Low |
| `workflows/source/video_minimax_h3_r2v.json` | 46473 | a79f1a8ae7d250f9da8b0f4e636e202526c37457b808e74fdf25de7ceed96574 | JSON | Workflow artifact | n/a | n/a | Low |
| `workstation/PLANNED.md` | 322 | 52c50022b93563314d06ce56d250ae187aefbf97fb1756eabfa22f3fd6d0f156 | Markdown | PLANNED layout doc (no implementation) | n/a | n/a | Low |
| `workstation/README.md` | 2038 | 0608905f9c682eb401604a2d59835cc928e9cf0c18064972a6e4242ab5656bf9 | Markdown | PLANNED layout doc (no implementation) | n/a | n/a | Low |
| `requirements-ci.txt` | 272 | b75be167bad311ea43d796e09e519fe92efb4afe1cddbec65df110ebea31e9c3 | Text | CI dependencies (requests/PyYAML pins from requirements.lock) | n/a | n/a | Low |
| `scripts/regenerate_manifest.py` | 3753 | 770abb80a688adf81d4809614a964ab07b6cda0b2095e62b254dbfd0e15f29c7 | Python | Deterministic SOURCE_MANIFEST.json generator | n/a | n/a | Low |
| `scripts/secret_scan.py` | 5012 | ea49216469315d043637a4a8111f69569d959210122164e5ef00cb41b6973cd8 | Python | Secret scan (stdlib; pattern scan, never prints values) | n/a | n/a | Low |
| `tests/fixtures/pristine-colab-cli-0.7.5/README.md` | 1220 | d83a68f0448d6c460b21d93d3e17d1d86a950a9f96c73c30ec5ab0f5f5e3ca9d | Markdown | Fixture provenance: pristine google_colab_cli 0.7.5 wheel | n/a | n/a | Low — pristine upstream CLI code, no credentials |
| `tests/fixtures/pristine-colab-cli-0.7.5/colab_cli/client.py` | 11772 | 7081d2c9decbc9a4cafb4ab89202c7e7a644f72bd74a7e846bffce5344839a06 | Python | Fixture: pristine CLI client.py (patch-validation negative control) | n/a | n/a | Low — pristine upstream CLI code, no credentials |
| `tests/fixtures/pristine-colab-cli-0.7.5/colab_cli/commands/automation.py` | 9805 | 229a11e791570c592291b41ab3c4f7d17719b6590b4032a0f0fd40f71dd94186 | Python | Fixture: pristine CLI automation.py (patch-validation negative control) | n/a | n/a | Low — pristine upstream CLI code, no credentials |
| `tests/test_drive_delivery.py` | 6661 | 8235e427092ffcca6fc4fc028ffbf91907d30b27146933b31903a3863b5435a8 | Python | Tests: Drive delivery integration (TASK-002 §4.5) | n/a | Y | Low |
| `tests/test_task_budget.py` | 9259 | 3b93a129f249659e9a2176b4481390ec3e168278f24503edddb38a79df9c540c | Python | Tests: per-task 30 CU budget isolation (TASK-002 §5) | n/a | Y | Low |
| `tests/test_watchdog_fail_closed.py` | 9318 | e1c5910d2613426e7490dbf7275cecaa37ee25d76b757c3257504ca294517f50 | Python | Tests: fail-closed watchdog + orphan preflight (TASK-002 §6) | n/a | Y | Low |
| `docs/issues/p0-fix-github-ci.md` | 1769 | 38f713c11f9e7368c811560d59b7a61ceb99ab2dceac00664bb256e8279efc40 | Markdown | Issue draft (TASK-002 §7) | n/a | n/a | Low |
| `docs/issues/p0-remove-personal-data.md` | 1591 | 2953cef4f5b19a6b53b0dc2a7984c266eae3c03d71a24995a711bcd054aba921 | Markdown | Issue draft (TASK-002 §7) | n/a | n/a | Low |
| `docs/issues/p1-fail-closed-watchdog.md` | 1831 | 325594e8fba4744d081ed9eb7147ec1a149acaddb90fe3fdb22f80e04fec1636 | Markdown | Issue draft (TASK-002 §7) | n/a | n/a | Low |

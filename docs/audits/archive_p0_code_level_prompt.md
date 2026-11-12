# 给 Muse 的代码级开发与授权任务书：MiniMax-H3 G4 + Drive 持久化权重仓库（P0）

## 0. North Star 与变更约束（不得重构）

- 仅 **Muse Linux → 官方 google-colab-cli → Google Colab GPU**；Google Drive v3 API 作为持久化权重仓库。
- 现有 `h3_controller.py` 负责 GPU 会话 / CU / stop；现有 `h3_remote_worker.py` 负责 Ref2VA。`reference_v2/` 是早期 `Video.zip` 参考代码，不能启动 Antigravity、不能转交 Mac。
- 本轮仅允许：本地 venv/CLI 依赖安装、离线代码修复与测试、官方帮助核对。**尚未授权** OAuth2、Drive 写入/37GB 模型传输、GPU 创建、远程推理、CU 购买。触达这些环节必须暂停请求用户批准。
- 不创建第二个 GPU manager，不静默修改预算硬停线 `153.82`，不删除历史缓存、旧日志、海报、剧本或用户模型。

## 1. 以本代码包的真实文件为基线

```text
h3_controller.py                 官方 CLI 包装、会话预算和清理；已新增在线缓存门禁
h3_remote_worker.py              远端 restore_weights → ComfyUI → A/B 视频
h3_contract.py                   4 权重契约，A/B Prompt，API JSON 节点绑定
h3_gpu_probe.py                  G4 95GiB 探测（GPU 真机待验）
assemble.py                      226 动画帧 + 48 原海报 → 274 帧、24fps
assets/original_poster.jpg       用户原图，不得修改；校验 SHA256
assets/storyboard.md             用户原始分镜，不得修改
reference_v2/                    原 Video v2 只读参考

# 本次已增加
 drive_depot.py                   Drive model depot CLI：account-check/init/seed/verify
 drive_api.py                     Google Drive v3 metadata + 上传后校验
 drive_resumable.py               Google Drive HTTP 308/Range 跨进程可恢复上传
 model_cache/verified_manifest.example.json
 tests/test_drive_depot.py        P0 无云端 Mock 安全契约测试
 tests/test_drive_resumable.py     官方 308 Range、跨进程 sidecar 恢复契约测试
```

## 2. 代码接口与硬性契约——不得自由发挥

### 2.1 授权（只实现/核验流程，不擅自登录）

**方式 A：统一 ADC，单次由用户自行完成 OAuth2（可作为优先候选）。**

- `gcloud auth application-default login --no-launch-browser --client-id-file=$HOME/private/desktop_oauth_client.json --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory,https://www.googleapis.com/auth/drive.file`
- 本机 `colab --auth=adc usage` / `sessions` / `whoami` 不新建 GPU。
- `python drive_depot.py account-check --colab-auth adc` 读取 Drive `about.get(fields='user(emailAddress)')` 与 Colab CLI `whoami`，仅比较账号、输出 bool，不展示邮箱、access token 或 refresh token。
- 使用独立、用户自己的 Desktop OAuth client ID；默认 gcloud client 的 cloud-platform Scope 不足以获得 Drive scope。自有 Client JSON 应存私人路径，不提交 Git。ADC 存储 refresh token 的 JSON 可能为明文，需要用户先确认 Muse VM 的安全边界。
- 同一个 OAuth 授权通过不等于 Colab 后端一定接受该 Desktop client，必须实际做只读 `usage` 验证；失败则停止，不偷偷改回 Mac。

**方式 B：两次 OAuth，Colab 官方原生 OAuth2 + Drive ADC。**

- CLI 用户终端：`colab --auth=oauth2 usage`，浏览器复制验证码并粘贴回原终端（绝不发到 Muse 聊天）。
- Drive ADC 独立授权，仅用于 Drive API，按前述 gcloud 命令。两个会话必须用同一 Google 账号，通过 `account-check --colab-auth oauth2` 阻断错误身份。
- CLI v0.7.5 或更新版的官方 OAuth2 token 使用 Keyring；没有可用 Linux Keyring 时可能无法持久化。不能复制 CLI keyring 私钥到 Drive ADC 文件。

**禁止方式 C**：让 Muse 用浏览器自动点击 Google 登录、访问 cookie、抓取授权码、点击 Colab Notebook 的运行按钮代替 CLI。

### 2.2 Drive model depot：以下函数不得更名或改写功能定义

`drive_depot.py`：

```python
# 操作返回前必须验证同一账户，拒绝服务账户冒充个人 Drive
account_matches(service, colab_auth: str) -> bool
# 只能在显式 allow_write 下建立 Google Drive 新专用目录
ensure_folder(service, parent: str | None, name: str, allow_write: bool)
# LIVE server file evidence；不能信 FUSE copy2 或 CACHE_READY.json
verify_drive_entry(service, record: dict) -> dict
# 对旧 manifest 重新远程核对 4 个 file id + size + server MD5 + parent
verify_ready(service, account_ok: bool, ready_path=READY) -> dict
# HF 仓库必须使用准确的 40位 sha，不允许 main/latest
seed(service, root_id: str, revision: str)
```

固定模型清单（路径/名称必须逐字一致）：

```text
diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors
text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
vae/minimax_h3_video_vae_int8_convrot.safetensors
vae/minimax_h3_audio_vae_fp32.safetensors
```

不使用 Turbo LoRA。约 37GB 总权重为历史估计，必须按真实 Hugging Face metadata/本地字节核算。允许 `drive.file` Scope 在同一账号下**新建本应用私有管理目录**，不能假定可以用该 scope 访问历史 Antigravity 创建的其他目录/旧 VAE。若旧 H3 目录返回 404，标记 ACCESS_UNKNOWN，不当作丢失；不可扩大 Drive 权限而不先征得批准。

状态机：

```text
NO_AUTH → ACCOUNT_CHECKED → FOLDER_READY
→ UPLOADING → VERIFYING → READY
                       ↘ ERROR / CACHE_INCOMPLETE
```

必须遵守：文件传输成功返回的 Drive `file_id` + 实际源端 `size,sha256,md5` + 重新从 Drive 服务端查询的 `size,md5Checksum,parents` 全部一致，才接受一个文件。四项全部一致并在线重新验证后才 `atomic_json(READY, manifest)`。上传中断后保留状态和已验证条目，绝不先发布 READY。默认不覆盖同名不同内容文件。优先使用 `drive_resumable.upload_with_persistent_session()`：在 `model_cache/.upload_sessions/` 中以 0600 保存上传 session URI，按 Google 官方 `PUT Content-Range bytes */SIZE` 查 308/Range 并续传；一次性未完成的单文件可以跨 Muse VM 重启恢复，条件是 `home`、本地权重及有效 ADC 仍在、Google session 未过期。会话过期（404/410）则记录失败并从服务端核查是否已有完整文件，避免重复写入。**本实现只有离线 Mock 验证，必须在无 GPU 的 Drive 小测试文件与真实大文件分别验收后才能认定通过 P0-02。**

保留两阶段服务端验证：`drive_depot.py verify` 与 G4 前 `h3_controller.py` 的 `verify_ready(...)`。不能为了节省几次 Drive API 调用移除后者。

### 2.3 Colab CLI 控制器：明确使用官方命令，不重造 HTTP 协议

CLI 官方仓库：`https://github.com/googlecolab/google-colab-cli`；安装版本以实际 `colab version`, `colab --help`, `colab exec --help` 为准。现有 `h3_controller.run_cli(cmd)` 使用 `subprocess.run([...],stdin=DEVNULL,shell=False)`，保留安全执行，不传授权码。

允许只读检查：

```bash
colab --auth=adc usage
colab --auth=adc sessions
colab --auth=adc whoami
python h3_controller.py plan
```

未来**必须用户明确授权**才能运行的调用图：

```text
h3_controller.test_or_run
  → validate_poster → patch_api_workflow API 格式契约
  → drive_depot.account_matches → drive_depot.verify_ready（LIVE）
  → inspect: colab usage + sessions → hard stop / budget / ownership
  → colab new -s UNIQUE_NAME --gpu G4
  → colab exec -s UNIQUE_NAME -f h3_gpu_probe.py
  → (已审计并另行授权的 Drive transport 挂载；无则停)
  → colab upload / exec / download
  → A 版写盘、SHA 与 ffprobe 验证后才执行 B
  → finally: colab stop -s UNIQUE_NAME
  → sessions + usage 复查，失败报警，不能报告完整成功
```

`h3_controller.py` 已增加：

```python
from drive_depot import drive_service, account_matches, verify_ready
drive = drive_service()
same_account = account_matches(drive, _COLAB_AUTH)
verify_ready(drive, same_account, Path(args.cache_manifest))
# 仅当前面全部成功后，才允许 colab new
```

**必须保持 GPU 单次审批四个参数** `--execute --confirm-gpu --authorize-task-gpu --allow-g4`，并要求 `--budget-cu` 指定本次预算。不得用 Prompt 文本代替参数审批，不能把历史剩余 11.45 CU 当新的 18 CU。

### 2.4 Drive 传输到 Colab 必须经实测，不得伪称端到端完成

原 `h3_controller.py` 目前调用 `colab dfs mount`，这来自第三方扩展、**不是上游官方 CLI**，而且请求的权限可能涵盖全部 Google Drive。Muse 应：

1. 读取扩展原始 GitHub 源码、许可证、OAuth scope 与 token 存储行为，报告准确来源及 diff；不要直接执行未经审计的 `curl | sh`。
2. 经用户许可才安装/授权该扩展，执行 CLI `dfs doctor/show` 检查，真实 mount 只能在下一次单次 GPU 授权时测试。
3. 如果用户不批准第三方扩展，改为官方 `colab drivemount` 的**受控交互流程**，实现有界等待与用户手动完成交互，先在小规模、获批准的会话中验证；当前代码的 `stdin=DEVNULL` 无法把授权码交互传给 CLI。
4. 不得把用户的 ADC refresh token/Drive 授权码直接复制到 Colab VM。
5. 两条路径都验证失败时报告 `BLOCKER: DRIVE_TRANSPORT_NOT_VERIFIED`，停在 GPU 申请前。

### 2.5 ComfyUI/H3 与工作流：这些是后续 GPU 阶段的固定参数

官方模板：`Comfy-Org/workflow_templates/templates/video_minimax_h3_r2v.json` 是 GUI 结构，不可直接提交 `/prompt`。先生成/API 导出 `workflow_api.json`，核对真实 `class_type` 和节点：`136 MiniMaxH3ReferenceToVideo`、`137/139 LoadImage`、`124 BasicScheduler`、`129 RandomNoise`、`127 UNETLoader`、`128 CLIPLoader`、`92 SaveVideo`、`146 PrimitiveBoolean`。

要求固定：`width=736,height=1312,length=226,fps=24,steps=25,ref_image_size=match`；`ref2va` 主权重而不是 `fl2va`；禁用 4-step Turbo；A/B 用不同 seed。输出 `shaonian_A/B.mp4`（可沿用现有文件名），每版动画追加 48 帧**原 JPG**，最后 274 帧，1080×1920、24 fps，海报之后不可加片尾。当前只验离线 API 参数绑定，不准执行 GPU 推理。

## 3. Muse 在本轮可实际执行的代码命令（均无远程 GPU）

```bash
cd ~/workspace/Muse_Colab_H3_Drive_v2
python3 -m py_compile drive_depot.py drive_api.py h3_controller.py h3_contract.py h3_remote_worker.py
python3 -m unittest discover -s tests -v
python3 h3_controller.py plan
python3 drive_depot.py --help
```

若用户随后独立批准**只读 Google OAuth**，才可运行 `drive_depot.py account-check`。若用户另行批准 Drive 空间/大规模传输，再进行 init/seed。不要擅自把 OAuth 授权与 GPU 计算授权混作一个批准。

## 4. 验收与禁止性行为

- 原 9 项 H3 Contract 离线测试必须保持通过；新增 Drive 测试全部通过；确认旧 READY、错账号、错 MD5、错 parent、未批准写入、大文件传输未批准都阻断。
- `h3_controller.py` G4 分配之前必须执行 `verify_ready`，不满足 `READY` 和四项 server metadata 就退出。任何模拟测试不得创造远程会话或消耗 CU。
- 真实 Drive 授权/G4/缓存 37GB 上传/ComfyUI `/object_info`/成片质量均标 **UNVERIFIED**，不得把“代码编译通过”等同“生产完成”。
- 输出：实际修改文件 diff、测试计数、官方 CLI 版本、授权是否完成（不含任何凭据）、模型仓库检验状态、剩余 P0 BLOCKER、下一次用户需要批准的具体操作与费用风险。
- 不扩张到 Wan2.2、SDXL、Mac、Antigravity 执行或新 Agent，多任务/长视频扩展等均非本次范围。

**本次执行到无 GPU 的本地代码与离线测试为止，等待用户决定授权方式；不要启动 OAuth 浏览器流程，不要创建任何 GPU 会话。**

# Muse → Colab G4 → MiniMax-H3：授权、Drive 持久模型仓库、A/B 视频全流程执行指令（v3）

> **North Star（锁定）**：用户 → Muse（唯一 Agent）→ Muse Linux 项目 Python 控制器 → Google 官方 Colab CLI → Colab G4 → MiniMax-H3 Ref2VA → A/B 成片 → 验证/下载 → 停止 GPU。Google Drive 是**真实的持久权重仓库**。Mac/Antigravity 不参与执行。
>
> 本轮用户已明确同意：①在 Muse 专用 Linux 账户的私人目录长期保存本项目所需 Google 凭据；②在**同一 Google 用户账号**下建立 Drive 仓库并传输四权重（约 37GB，实际以 Hugging Face 为准）；③在所有技术、安全与预算门禁通过后，尝试一次《少年壮志不言愁》A/B 全视频真实制作。**不授权购买 CU、增加项目预算或绕过门禁。**
>
> 背景证据：2026-11-09 MiniMax-H3 复盘 G4 分配曾成功，但模型持久化失败、旧 READY 假阳性、A/B=0/2；当前历史余额基线 165.27 CU、项目历史累计花费 6.55 CU、历史预算上限 18 CU、硬停余额 153.82 CU，后续必须实时核实。用户原图 `assets/original_poster.jpg` SHA-256 为 `bd6d45312f340234d0fa06af0de5284949b2196f280677dd44468a300075c7bb`，剧情详见 `assets/storyboard.md`。

## 0. 先核对现有代码，不新建架构

项目位置 `~/workspace/Muse_Colab_H3_Drive_v3/`（若本包解压名称不同，用实际目录）。先读取：

- `auth_support.py`：Colab CLI 优先从本 venv 解析，ADC 只查权限元数据。
- `drive_depot.py`：`account-check / init / smoke / seed / verify`；完整 Drive scope 的程序化接口**只允许写本项目独立 depot 子树**。
- `drive_resumable.py`：跨进程 308/Range、8MiB、0600 sidecar；如会话失效则按服务端记录恢复，不虚构成功。
- `drive_api.py`：Drive 服务端 `file_id/parents/size/md5Checksum`，SHA-256 有返回时一并校验。
- `h3_controller.py`：唯一 G4 管理器、旧项目预算和 stop；已移除第三方 DFS 硬依赖，改为官方交互式 `colab drivemount`，**无 TTY 时在 GPU 前拒绝**。
- `h3_drive_probe.py`：Drive 挂载后四权重可见性和字节数探针，在安装 ComfyUI 前执行。
- `h3_remote_worker.py`：Colab NVMe 复制与 SHA-256、ComfyUI、A/B 推理和 MP4 输出。
- `build_api_candidate.py`：将官方 UI workflow 转换为**候选** API JSON，不是未经真机验收即可付费运行的保证。
- `h3_contract.py` / `assemble.py`：原有 226+48 帧、角色、原海报硬约束。

不得覆盖用户 Muse 内更晚的实现；先用 `diff -ru` 或 git diff 比对，再将 v3 必要补丁增量合入。如果发现新代码更完整，以严格门禁和实际测试为准。

本地先执行（无云端改动）：

```bash
cd ~/workspace/Muse_Colab_H3_Drive_v3
source .venv/bin/activate  # 如还未创建，则先 python3 -m venv .venv && pip install -r requirements-local.txt
python -m py_compile auth_support.py drive_depot.py drive_resumable.py h3_controller.py h3_drive_probe.py build_api_candidate.py
python -m unittest discover -s tests -v
python h3_controller.py plan
```

记录原有 24 项与 v3 新增项的实际结果，不得根据此文声称已在 Muse 复跑。

## 1. 首次授权：统一 ADC，持久保留 Muse 用户环境；必须由用户本人交互

用户已批准**在 Muse VM 存储长期凭据**和申请**完整 Drive**访问。Muse 可以安装受信任的官方 Google Cloud CLI，但不得查看/输出 access token、refresh token、authorization code、Google 密码或浏览器 cookie，也不能把凭据提交 Git/日志/任务记录。使用 Muse 已连接的 Drive 插件核查同账号的原海报、剧本和历史目录**可见性**，但不要试图从插件提取秘密或假定插件 Token 可供 `google.auth.default()` 使用。

依照 Google 官方 Cloud SDK 安装文档安装 `gcloud`，优先在 `~` 下，不用会被重建清除的 `/etc`、`/tmp`。`gcloud --version` 后查验命令帮助。用户在 Google Cloud Console 创建**自己的 Desktop OAuth 客户端**，启用 Drive API、配置同意屏幕/测试用户及所请求的 Drive Restricted Scope（如受限授权被 Google 拒绝，则如实报告，不能借他人客户端绕过），将下载的 Desktop OAuth 客户端 JSON 安全放至 `$HOME/private/desktop_oauth_client.json`，目录 0700、文件 0600。

让用户在 Muse **交互式 shell** 内执行下面**一整条正确格式的命令**（不要在聊天框输入验证码）：

```bash
umask 077
mkdir -p "$HOME/private" && chmod 700 "$HOME/private"
gcloud auth application-default login \
  --no-launch-browser \
  --client-id-file="$HOME/private/desktop_oauth_client.json" \
  --scopes="openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/userinfo.profile,https://www.googleapis.com/auth/colaboratory,https://www.googleapis.com/auth/drive"
```

向用户明确说明：授权码**粘贴回发起命令的原始终端提示符**；不是 Muse 聊天。自有 OAuth 客户端若处于 External/Testing，非基础资料 scope 的 refresh token 可能仅 7 天有效，必须核实 Cloud Console 状态和真实 refresh 行为，不可仅因 JSON 存在就承诺永久有效。

授权后，运行（只读，不创建运行时）：

```bash
export PATH="$PWD/.venv/bin:$PATH"
python -c 'from auth_support import audit_user_adc_file; print(audit_user_adc_file())'
colab --auth=adc version
colab --auth=adc usage
colab --auth=adc sessions
python drive_depot.py account-check --colab-auth adc
```

验收：`account-check` 输出 `same_google_account=true`；usage 能得到余额、费率和会话；原海报/剧本的 **Drive File IDs** 在 Muse Drive 插件与 API 侧均能读取；ADC 文件只允许当前用户读写（0600），不把内容打印出来。若 Colab `whoami`/Drive API 不一致、401/403、scope 不足或账号不一致，**STOP_AUTH**，不要授权 Google 之外的第三方工具，不创建模型仓库、不申请 GPU。

## 2. 用 Drive v3 建立真实持久仓库（无 GPU，用户已批准本轮传输）

不得认为 `CACHE_READY.json` 或本地 `/content` 下载就是 Drive 成功。先用已授权的官方 Drive API 创建或复用一个根目录：

```bash
python drive_depot.py init --colab-auth adc --allow-drive-write
python drive_depot.py smoke --colab-auth adc --allow-drive-write
```

`init` 必须幂等，先查找 `My Drive/Muse-H3-Model-Depot`；成功才保存真实 root ID。`smoke` 将一个极小 canary 上传至 `_checks/smoke_test.bin`，比较服务端 `file_id、parents、size、md5Checksum`；这一步失败不得开始模型传输。**优先用现存两份 VAE：只有 Drive API 能看到历史 File ID、已下载 HF 同 revision 的本地 SHA/MD5 与旧文件服务端大小/MD5 完全一致时，才执行 Drive 服务端 `files.copy` 到新 depot，验证新 file ID。** 旧文件 404 视作 `ACCESS_UNKNOWN`，可改从 HF 正式源重新 seed，但不能当成已证明删除。

四文件固定清单：

```text
diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors
text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
vae/minimax_h3_video_vae_int8_convrot.safetensors
vae/minimax_h3_audio_vae_fp32.safetensors
```

固定 Hugging Face 仓库 `Comfy-Org/MiniMax-H3`；先由 `HfApi().model_info(...).sha` 获取**真实完整 40 位 commit SHA**，核对四个文件实际存在、大小、LFS SHA-256、仓库许可证；禁止用 `main/latest` 作为已固定版本。记录 revision 后才下载：

```bash
HF_REV=$(python - <<'PY'
from huggingface_hub import HfApi
m=HfApi().model_info('Comfy-Org/MiniMax-H3',files_metadata=True)
assert len(m.sha)==40, 'Missing pinned commit hash'
print(m.sha)
PY
)
python drive_depot.py seed --colab-auth adc \
  --hf-revision "$HF_REV" \
  --allow-drive-write --allow-large-transfer
python drive_depot.py verify --colab-auth adc
```

这里的 seed 传输是真实约 37GB 级操作，可能长时间运行，占用 Muse home、带宽和 Drive 空间；**用户本轮已授权这项模型仓库操作**，无需为此重复要求允许，但须实时检查空闲磁盘、现有资产和传输状态。上传可跨进程续传的条件为 source 文件、ADC 与 session URI 仍有效；会话过期不自动覆盖任何文件。四个权重都必须经过服务端文件 ID/大小/父目录/MD5 校验；新 v3 还在 Drive 根目录上传并校验 `READY_<40位HFrevision>.json`，同时将本地 `verified_manifest.json` 视为同一字节内容的副本。之后调用 `verify` 再次实时核对四文件 + Drive READY 文件。缺任何一项则 `CACHE_INCOMPLETE`，不得进入 G4。

## 3. 官方 ComfyUI Ref2VA 模板与 A/B 剧本（不自由发挥）

确保 `assets/original_poster.jpg` 的 SHA-256 不变，`assets/storyboard.md` 不变。下载官方模板 UI JSON：

`https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json`

不要直接提交 UI JSON。先使用包内 `build_api_candidate.py` 做严格**候选**转换：

```bash
python build_api_candidate.py \
  --gui model_cache/video_minimax_h3_r2v.json \
  --out workflow_api.json
```

然后 `python -c 'import json; from h3_contract import patch_api_workflow; j=json.load(open("workflow_api.json")); patch_api_workflow(j,"A"); patch_api_workflow(j,"B"); print("API_STATIC_CONTRACT_OK")'`。

**候选 API 不等于 ComfyUI 真实兼容。** 付费 G4 上必须先验证 `/object_info` 确认运行时节点和输入 schema；任何 prompt schema 拒绝直接停止而不是忽略错误。固定 official Ref2VA 模型和节点（`136/137/139/124/129/92` 等）；`736×1312、226帧、24fps、25steps、ref_image_size=match`，禁用 Turbo；A/B 差异由原分镜控制，禁止静默减帧、降 steps、改模型、重绘最终海报。

本项目成片严格验收：A 两次明显不同的冲锋/闪避；B 先狼狈跑后找回从容；两版均为最后举篮球，追加同一张原海报的 48 帧、无后续卡；输出各 274 帧、1080×1920、24fps、约 11.42 秒。技术解码通过不等于剧情/画质通过，需抽帧 QA。

## 4. G4 条件性真实生成（用户允许，但不得突破旧预算）

**本轮用户已同意：若仓库真实 READY、授权与模型/ComfyUI 必要检查通过，尝试一次 A/B 实际制作。** 不得把本次授权理解为无限 CU、重复 G4、购买套餐或使用高阶备用 GPU。若 GPU 工作流还缺真实节点契约、无法获得交互式终端授权官方 Drive 挂载、当前账户不同或余额不足，则直接停在 `NO_GO`，报告阻碍，不通过第三方 `colab dfs` 绕过。

执行前：重新运行 `colab --auth=adc usage`、`sessions` 和 `drive_depot verify`；没有外部会话，余额与历史硬停线 153.82 CU、历史已耗 6.55 CU 的剩余项目额度完全匹配；G4 实时费率 ≤ `--max-rate-cu-h` 上限。软件预算取 **最多 10.00 CU**（小于历史剩余 11.45 CU，保留安全余量），单任务最多 3600 秒，实际可用预算按实时 rate 计算；这只是软件限制，计费与停止故障仍可能产生偏差。当前实时余额若低于 `153.82 + 10.00 CU`，停机并报告，不要自改硬停线。

在真实 Muse **可交互 TTY 的终端**运行下面命令；这不是让 Agent 在无 TTY shell 中后台运行。在申请 GPU 前，本控制器核对 TTY 与官方 `drivemount --help`。启动 G4 后，执行官方 `colab drivemount -s SESSION`，如还需要交互式 Drive 许可，用户只在终端配合；挂载失败则在 finally 停止 GPU。不得安装 `deluxebear/colab-dfs-ext` 或把 Muse ADC refresh token 复制到 Colab。

```bash
# 用 git ls-remote / git rev-parse 记录真实 ComfyUI commit：
COMFY_REF=$(git ls-remote https://github.com/Comfy-Org/ComfyUI.git HEAD | awk '{print $1}')
test "${#COMFY_REF}" -eq 40 || exit 1

python h3_controller.py run \
  --colab-auth adc \
  --workflow-api "$PWD/workflow_api.json" \
  --cache-manifest "$PWD/model_cache/verified_manifest.json" \
  --drive-cache-dir /content/drive/MyDrive/Muse-H3-Model-Depot \
  --comfy-ref "$COMFY_REF" \
  --budget-cu 10.0 \
  --max-rate-cu-h 12.0 \
  --max-wall-sec 3600 \
  --approve-interactive-drive-mount \
  --execute --confirm-gpu --authorize-task-gpu --allow-g4
```

这段命令会**真实创建付费 G4**，只能在当前用户已允许的上述条件下执行，不允许改变预算阈值或加入 `--keep`。控制器执行图固定：

`账户+四权重+云端 READY 服务端核验 → CU/sessions/预算 → G4 new → 硬件探针 → 费率检查 → 官方 Drive 挂载 → 传入 manifest/海报/工作流 → Drive 可见性探针 → NVMe 哈希校验 → ComfyUI /object_info → A 模型推理 → 下载/校验 A → B 模型推理 → 下载/校验 B → finally stop → sessions/usage 二次核查`。

不要为了赶预算假称 "A/B 均成功"。若 A 完成但 B 未完成，保留已下载 A、记录 B 未交付，停止 GPU。若出现 HTTP503，有限等待并再次核对迟到的会话，不无限重试。若 G4 费率异常或无法确认停止，优先止损、发出 CRITICAL 告警。每次账务记录分即时差额、费率估计和最终确认，缺证据时最终确认为 `null`。

## 5. 交付标准与必需报告

- 阶段一：Colab/Drive/插件账号一致性检查，ADC 文件权限，OAuth 状态；不得输出秘密。
- 阶段二：Google Drive `Muse-H3-Model-Depot` 真实 root ID、四文件的 `file_id/bytes/server MD5/source SHA/HF revision/parent`、远端 READY JSON ID 及最新在线验证结果；上传失败必须写 `CACHE_INCOMPLETE`。
- 阶段三：ComfyUI 实际节点 `/object_info` 兼容性（未验证必须如实标注），G4 实际型号与 VRAM、CU/小时费率、总耗时。
- 阶段四：A/B MP4 文件路径、SHA-256、FFprobe 帧数、帧率、尺寸、时长、各阶段失败原因和抽帧剧情 QA；不要凭两个文件存在就宣布画质合格。
- 阶段五：运行结束后 `sessions/usage` 复核、GPU 是否确认停止、CU 三口径，是否有需人工清理的孤儿会话。
- 全程不得改变 Muse 唯一 Agent 架构，不需要 Mac/Antigravity；不得丢失历史模型文件或改动现有用户 Drive 资产。

## 6. 支持性依据

- Google 官方 Colab CLI: https://github.com/googlecolab/google-colab-cli
- Colab OAuth2/ADC/Drive 挂载: https://github.com/googlecolab/google-colab-cli/blob/main/docs/04_automation_and_utility.md
- gcloud ADC 自定义 OAuth scopes: https://cloud.google.com/docs/authentication/application-default-credentials
- Google Drive Restricted Scope: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- MiniMax H3 官方 ComfyUI UI 模板: https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json
- 历史失败复盘: https://app.notion.com/p/413ad272535b814f858ce20bbb861633

**真实性边界**：此 v3 代码包已在当前 ChatGPT 容器内通过离线测试，但在 Muse 的 Google 账户、Drive 37GB、Colab G4、ComfyUI 和完整 A/B 视频上尚未实测。Muse 必须以上述验证证据逐层替换 `UNVERIFIED`，不可凭本任务书宣称已经执行。

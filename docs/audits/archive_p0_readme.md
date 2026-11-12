# Muse → Google Colab CLI → MiniMax-H3 (G4) + Google Drive Model Depot

**版本：P0 认证与持久化模型仓库代码包，2026-11-10。状态：本地离线验证；没有执行 Colab 授权、Drive 上传、G4 或 H3 真机推理。**

## North Star（不可改变）

用户 → **Muse（唯一 Agent）** → Muse Linux 的 Python 控制器 → **Google 官方 Colab CLI** → Colab G4 GPU → MiniMax-H3 Ref2VA → A/B MP4 → 下载与校验 → 停止 GPU。Google Drive 是持久化权重仓库，不是 Antigravity 运行时。Mac 与 Antigravity 不参与控制。

参考：`reference_v2/` 是用户原 `Video.zip` 中的安全预算、会话管理思路；不启动其 Mac 路径。MiniMax-H3 历史复盘证实 G4 曾成功分配，但四权重未完成 Drive 持久化且未生成 A/B，旧 `CACHE_READY.json` 必须视为无效。

## 这次实际新增 / 修改的控制代码

| 文件 | 作用 | 有无真实授权或 GPU 验证 |
| --- | --- | --- |
| `drive_depot.py`（新增） | 同一账号校验、`drive.file` 专用文件夹、固定 HF revision 下载、分块上传、逐文件 Drive 服务端验证、四文件全部通过才原子提交 READY | 离线 Mock 验证，无实时云端执行 |
| `drive_api.py`（复用） | Drive v3 的 file id、size、MD5 和可选 SHA-256 服务端元数据，以及上传后校验 | 尚未实传 37GB |
| `drive_resumable.py`（新增） | 按官方 308/Range 协议实现 8MiB chunk 跨进程续传，上传 URI 保存在私有 0600 sidecar；真实 Drive 服务端尚未验证 | 离线 Mock 验证 |
| `h3_controller.py`（增强） | 保留 GPU 预算与停机逻辑；在 `colab new` 前强制再次调用 `drive_depot.verify_ready` 和 `account_matches`；新增 `--colab-auth oauth2/adc` | 无 GPU 运行 |
| `h3_contract.py`（复用） | 四权重列表、剧本 A/B Prompt、ComfyUI API节点契约 | 离线验证 |
| `h3_remote_worker.py`（复用） | 已挂载 Drive → Colab NVMe 校验拷贝 → 官方 H3 工作流 → A/B 生成 | ComfyUI 与 CUDA 真机尚未验证 |
| `tests/`（扩展） | 原 9 项 H3 离线测试 + 新增 Drive/同账号/假 READY 阻断测试 | 通过；不代表在线授权/推理 |

### 模型仓库结构（同一个 Google 用户账号新建专用文件夹）

```text
Google Drive / My Drive / Muse-H3-Model-Depot/
├── diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors
├── text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
└── vae/
    ├── minimax_h3_video_vae_int8_convrot.safetensors
    └── minimax_h3_audio_vae_fp32.safetensors
```

**选择创建新的、只由本项目 OAuth 客户端管理的文件夹，是为了能用较小的 Google Drive `drive.file` 权限访问。** `drive.file` 并不自动授予其他应用创建的旧 H3 缓存访问权；旧两个 VAE 可留在原目录，不能因为同一 Gmail 就假定 API 可读旧文件。要复用旧缓存需要用户用 Google Picker 等方式明确授权对应文件给本应用，或另行批准较宽的 Drive 权限；本包不会自动提权。

## 一、初始离线验证：无需 OAuth / CU

```bash
cd ~/workspace/Muse_Colab_H3_Drive_v2
python3 -m unittest discover -s tests -v
python3 h3_controller.py plan
```

依赖与环境安装应放项目 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-local.txt
colab version
```

Python 和包版本应由 Muse 记录并在首次成功测试后锁定，不能宣称未运行过的特定组合已通过。

## 二、Colab 授权：不是让 Muse 控制 Google 登录网页

**方式 1：推荐在能安全保护 ADC 的可信 Linux 环境中，采用一次人工作业完成的统一 Google 用户 ADC OAuth（候选方案，尚需实际验证 Colab 后端接受）。**

用户先在 Google Cloud Console 注册自己的 Desktop OAuth client，配置所需同意范围与测试用户，下载客户端文件放到私人目录（不得提交 Git）。必须安装受信任的 `gcloud`。在 Muse 的**交互终端**运行：

```bash
# <PATH> 是您个人 Google Cloud Desktop OAuth client JSON 的真实私有路径
umask 077
gcloud auth application-default login \
  --no-launch-browser \
  --client-id-file="$HOME/private/desktop_oauth_client.json" \
  --scopes="openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory,https://www.googleapis.com/auth/drive.file"
colab --auth=adc usage
colab --auth=adc sessions
python drive_depot.py account-check --colab-auth adc
```

Google 会显示浏览器授权链接；由用户本人登录并将授权码输入**原始交互终端**，而非 Muse 聊天。`account-check` 只打印 `same_google_account=true/false`，不显示邮箱。若 Colab ADC 访问报 401/403、身份范围不正确或 Drive 403，请停下诊断，不要假装授权成功。Google 官方说明针对 Drive 的 ADC 自有 OAuth client ID 需要 `--client-id-file`，不能只使用默认 cloud-platform 授权。

**重大安全取舍：** `gcloud` 用户 ADC 缓存为 `$HOME/.config/gcloud/application_default_credentials.json`，含明文 refresh token；文件权限 0600 只是基本保护，无法抵御可访问同一 Linux 账户的恶意进程。若 Muse 运行时不可信或他人可读，则**不要存放用户的全账户 Google 凭据**，改为隔离的可信主机/受限账号或先停在离线阶段。服务账号不等同于你的个人 Drive/Colab 账号，不能作为默认替代。

**方式 2：分开授权，Colab CLI 原生 OAuth2 + 独立的 Drive ADC。**

```bash
colab --auth=oauth2 usage        # 原始终端会显示 Google OAuth URL 并要求粘贴验证码
colab --auth=oauth2 sessions
# Drive 另按上面的 gcloud 命令授权：Drive scope 至少 drive.file
python drive_depot.py account-check --colab-auth oauth2
```

官方 Colab CLI 的 `oauth2` 方式用系统 Keyring 保存 refresh token；在无可用 Linux Keyring 的 VM 上可能每条命令重新授权。两组 OAuth 必须用同一 Google 用户身份；独立登录不代表身份自动相同。`--auth=adc` 或 `--auth=oauth2` 的 CLI 选择与 Drive API 权限是不同的概念。无论哪个方式，本代码不要求 Muse Agent 读取或转述授权码。

**不推荐的方式：** 让 Muse 打开 Google 网页、模拟点击 Notebook 的运行按钮、复制 cookie/验证码。它不是可稳定审计的 Colab 管理接口。官方 CLI + 用户亲自完成 OAuth 浏览器同意 + API/命令行自动控制才是目标。

## 三、模型仓库（授权后、无需 GPU，第一次大约 37GB 的下载和上传需单独批准）

先只验证用户身份，再由用户明确允许创建专用 Drive 文件夹：

```bash
python drive_depot.py account-check --colab-auth adc
python drive_depot.py init --colab-auth adc --allow-drive-write
```

在 `Comfy-Org/MiniMax-H3` 的 Hugging Face 官方仓库查证固定 **40 位 Git commit SHA**，不要使用浮动 `main`，不要编造 revision：

```bash
# 这将实际下载并向 Google Drive 上传约 37GB 模型，可能消耗带宽和 Drive 空间，
# 但不请求 Colab GPU。请另行确认大流量/存储授权。
python drive_depot.py seed \
  --colab-auth adc \
  --hf-revision '<EXACT_40_HEX_HF_COMMIT>' \
  --allow-drive-write --allow-large-transfer
python drive_depot.py verify --colab-auth adc
```

两阶段提交：`UPLOADING → VERIFYING → READY`；模型需全部具有来源 revision、真实 Drive file ID、parent ID、size、source SHA-256、服务器 MD5，并再次读取 Google Drive API 验证后，才能原子写入 `model_cache/verified_manifest.json`。这份本地 manifest **不是云端真实存在的替代品**，在启动 G4 前 `h3_controller.run` 还会再次在线校验 4 个文件。上传基于 `drive_resumable.py` 的官方 308/Range 续传协议；`model_cache/.upload_sessions/` 私有文件权限为 0600，保存敏感的上传 session URI（不可提交 Git）。若同一文件、原始数据和凭据仍在持久化 home，且 Google 上传 session 尚未过期（文档约一周），可以在 Muse VM 更换后重新询问服务端实际已接收字节并续传；会话过期/源文件丢失/Drive API 返回异常时必须失败并重新协调，不保证任意情况下无重传。**此路径仅通过 Mock 测试，尚未在真实 Drive 大文件上传中验收**。

## 四、Colab 访问 Drive 模型仓库：本轮仍存在必须明确验收的传输门禁

已完成的 `h3_remote_worker.py` 从 `/content/drive/MyDrive/Muse-H3-Model-Depot/` 拷贝权重并在 Colab NVMe 再做 **source SHA-256** 校验。当前 `h3_controller.py` 的 GPU 流程使用 `colab dfs mount`，但 `colab dfs` 是 **第三方扩展**，不属于官方 CLI；不能让 Muse 直接 `curl | sh` 静默安装，也不能未经授权勾选整个 Drive 全盘权限。

可供用户审批的访问路径：
- **第三方 DFS 扩展**：审计仓库、确认运行时版本与 OAuth scope、显式用户同意授权及持久保存机制，运行 `colab dfs doctor/show` 验证，然后由控制器的 `dfs mount` 挂载。**此路径在本包尚未真机验证；未通过就不可运行 G4。**
- **官方 `colab drivemount -s SESSION`**：需要在已分配的 Colab 会话中交互式 Drive 授权，可能卡住或付费等待；当前控制器的 `subprocess.DEVNULL` 不支持代输入码。要改用此模式，需由 Muse 明确新增交互式状态机、有限超时和测试，不能只把命令名替换。
- 其他基于官方 Drive v3 API 的**远程直接下载**可作为独立扩展，但需设计最小权限短期 token 的安全传递与撤销机制，不能把个人 refresh token 上传到 Colab VM。

当没有真实可用的挂载路径时，**即使 `verified_manifest.json` 显示 READY，也不可申请付费 GPU。**

## 五、生成目标与真实执行边界

真实作品（以原始 JPG 与 storyboard 为准）：A/B 两版；动画 736×1312、226 帧、24 fps、25 steps，最终各 274 帧、1080×1920、约 11.42 秒；最后 48 帧使用**未改动的原始海报**。官方 Comfy-Org Ref2VA 模板需要导出 API Format JSON，才能给 `/prompt` 使用，现有 worker 不接受 UI JSON。已有 `h3_contract.patch_api_workflow()` 绑定官方节点 `92,124,127,128,129,136,137,139,146`，禁止 Turbo 与静默降质。

首次 GPU 测试**还没有授权**。历史 H3 预算基线为 171.82 CU，已报告消耗 6.55 CU，历史硬停线 153.82 CU、剩余预算 11.45 CU。历史 G4 费率约 10.05 CU/h，**不保证新 G4 有货、费率不变或可以按某“20 分钟 6 CU”的网络案例成片**。每次新执行必须实时检查当前 `usage/sessions`，核对余预算与身份，且用户对指定任务、GPU、CU 上限明确批准。不得因为重跑或新 Run ID 重置项目预算。

## 官方与项目参考

- Google Colab CLI https://github.com/googlecolab/google-colab-cli
- CLI OAuth2 / ADC https://github.com/googlecolab/google-colab-cli/blob/main/docs/04_automation_and_utility.md
- Google ADC / OAuth client ID https://docs.cloud.google.com/docs/authentication/application-default-credentials
- Google Drive 最小 OAuth scope https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- 官方 MiniMax H3 ComfyUI https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json
- 2026-11-09 H3 事故复盘 https://app.notion.com/p/413ad272535b814f858ce20bbb861633

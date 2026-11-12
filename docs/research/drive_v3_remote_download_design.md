# Drive v3 API 远端直下方案 — 设计 + 安全验证

- **文档性质**：仅设计与安全验证，不含实现代码，不改动任何现有文件。本轮不创建 GPU、不写 Drive、不复制任何长期凭据。
- **日期**：2026-11-11（UTC）
- **依据文件（本地只读）**：`drive_api.py`、`drive_depot.py`（`drive_service` / `verify_ready` / `upload_one`）、`h3_remote_worker.py`（`restore_weights`）、`auth_support.py`、`drive_resumable.py`、审计汇总 `2026-11-11_H3_AB生产多轮尝试审计汇总.md`、vendored 官方 CLI 0.7.5（`colab_cli/commands/automation.py`）、模型 manifest `model_cache/verified_manifest.json`。
- **零密钥真值**：本文不含任何 token、授权码、授权链接、代理凭据。Drive 文件 ID 请以 `model_cache/verified_manifest.json` 为准，本文只引用路径、大小与 SHA-256。

---

## 0. 现状与设计目标

### 0.1 当前传输路径（挂载路径）

1. 分配 GPU 后，控制器执行官方 CLI `colab drivemount`。
2. 远端 kernel 执行 `drive.mount('/content/drive')`，触发 `dfs_ephemeral` 认证请求。
3. CLI 的 `colab_request_hook`（`drivefs_hook`）拦截该请求，向 `/tun/m/credentials-propagation/{endpoint}` 发起凭据传播（`authtype=dfs_ephemeral, version=2`，先 `dryrun=true` 试探，未授权时打印授权链接并等待用户在浏览器完成授权，再 `dryrun=false` 正式传播），随后经 WebSocket stdin 通道回 `input_reply`，kernel 继续挂载。
4. 挂载成功后，`h3_remote_worker.py` 的 `restore_weights()` 把 4 个文件从 FUSE 挂载点以 16 MiB 分块复制到 ComfyUI 的 `models/` 目录，写 `.part` 临时文件，全文 SHA-256 校验通过后原子替换；已存在且校验通过的文件直接复用。

### 0.2 当前挂载路径的问题

- 远端 `drive.mount()` 默认 `timeout_ms=120000`，从 cell 启动计时、含等待授权时间；第 6、7 跑授权分别在提示出现后约 9 分钟、约 7 分钟才完成，均抛 `ValueError: mount failed`。
- **已观察到的修复状态**：本项目 vendored 的 CLI 0.7.5（`colab_cli/commands/automation.py`）已包含 2026-11-11 的挂载修复——`drivemount()` 远端执行 `drive.mount(path, timeout_ms=600000)`，本地 `INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 660`，留 60 秒传播与回包余量。该修复尚未经过一次真实生产跑验证。
- FUSE 复制速率从未实测；挂载本身仍依赖一次交互式浏览器授权 ceremony，授权耗时直接计入 GPU 计费时间（第 6 跑 1.11 CU、第 7 跑 0.74 CU，大部分消耗在等待授权与挂载超时上）。

### 0.3 本设计的目标

设计一条**不依赖 FUSE 挂载**的备选传输路径：Colab VM 直接调用 Drive v3 API（`files.get alt=media`，Range 断点续传），把 4 个模型文件下载到本地 NVMe，再复用现有的 SHA-256 校验与 `restore_weights` 的落盘逻辑。设计目标：

- 彻底去掉交互式授权 ceremony 对 GPU 计费时间的占用（token 在本地预先 mint，远端下载可立即开始）；
- 去掉 FUSE 这一层故障面（挂载超时、DriveFS 读错误、FUSE 读速率不确定）；
- 安全边界不低于现有路径：长期 refresh token 永不离境、下载文件校验标准不变、失败时可回退挂载路径。

### 0.4 传输对象（以 manifest 为准）

| # | 路径 | 大小（字节） | SHA-256（前 16 位，完整值见 manifest） |
|---|---|---|---|
| 1 | `diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 20,970,379,616 | `9255f52b6677845a…` |
| 2 | `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15,687,142,551 | `35a88d51044231fe…` |
| 3 | `vae/minimax_h3_audio_vae_fp32.safetensors` | 605,254,808 | `8e505d95dd1561d4…` |
| 4 | `vae/minimax_h3_video_vae_int8_convrot.safetensors` | 2,811,065,184 | `52a2c8c73583c86e…` |
| **合计** | | **40,073,842,159（37.32 GiB）** | HF revision 已 pin：`54472221d27c695fca9621b0c3ed52f06c4af707` |

- Manifest 状态 `READY`，验证方式 `drive_v3_server_api`，2026-11-10 22:36 UTC 发布；depot 根为 `Muse-H3-Model-Depot`。
- **本设计不重新初始化、不重复上传、不改动 depot**：下载只读 `files.get`，不调用任何写 API。

---

## 1. 认证方案比较

远端 VM 调用 Drive v3 需要一个有效的 Google OAuth access token。三个候选方案如下。

### 1.1 方案（a）：VM 自身 ADC / `auth.authenticate_user()`

- **机制**：沿用官方 CLI `colab auth` 已有的路径——远端执行 `auth.authenticate_user()`（`USE_AUTH_EPHEM=0`），用户在浏览器完成一次 OAuth 授权，VM 内获得该 Google 账号的凭据，`google.auth.default()` 可直接用于 Drive v3 调用。
- **优点**：官方路径，无 token 跨机器传输；凭据由 Colab runtime 管理，会话结束即失效；无需本地参与 mint。
- **缺点**：
  - 仍需一次交互式浏览器授权 ceremony，授权等待时间照样计入 GPU 计费——本设计“去掉 ceremony”的目标只实现一半（去掉了 FUSE，没去掉等人）。
  - 授权的 scope 由 Colab 的 auth 流程决定，调用方无法精确收窄到只读；VM 内凭据的实际 scope 与有效期在本文写作时**未实测确认**，须以一次 smoke test 为准。
  - VM 内凭据的生命周期与会话绑定；若下载中途会话重建，须重新走 ceremony。
- **结论**：可用作回退，不作首选。

### 1.2 方案（b）：本地 mint 短期 access token，传给 VM（推荐）

- **机制**：控制器在 Muse VM 本地用已有 ADC（`google.auth.default()`）刷新出一个 access token，仅授予 `drive.readonly` scope，有效期约 1 小时；通过已有的 `colab exec -f` 文件上传通道，把 token 以 0600 文件送到 VM（详见 §2.3）；远端 downloader 只读该文件，用它作 `Authorization: Bearer` 调用 Drive v3；下载完成后立即删除 token 文件并验证删除。
- **优点**：
  - **最小权限**：只读、无写能力、单用途、1 小时自过期（§2.1）。
  - **长期 refresh token 永不离境**：出境的只是一个 1 小时有效的 access token（§3 硬规则）。
  - **去掉交互 ceremony**：mint 在本地完成，不需要用户在浏览器点授权，GPU 分配成功后下载可立即开始；授权等待不再占用 GPU 计费时间。
  - **可验证**：mint 后本地可立即用 tokeninfo 验证 scope 与过期时间（§2.2）；远端可用一次只读 `files.get` 元数据调用做连通性 smoke test，再开始 40GB 下载。
  - **可撤销**：用户可在 Google 账号安全页随时撤销授权；token 过期后自动失效。
- **缺点**：
  - Token 须经过控制通道进入 VM，存在泄露面（§2.3、§7 逐项防范）；泄露窗口为 1 小时，且 token 可读全部 Drive 内容（readonly 的代价，见 §2.1 取舍说明）。
  - 40GB 下载必须在 token 有效期内完成，或中途由控制器重新 mint 并推送新 token（§2.4）；速率低于约 12 MB/s 时单 token 不够用（§5.4）。
  - 依赖本地 ADC 仍然有效。ADC refresh token 为 Testing 模式，存在约 7 天过期风险（2026-11-10 授权）；若 ADC 已过期，mint 在第一步即失败（§8 阻塞项）。
- **结论**：**推荐方案**。它是三个方案中唯一同时满足“最小权限 + 无长期凭据出境 + 无交互 ceremony”的方案；其风险全部是可验证、可缓解的（速率实测、ADC 有效性检查、token 删除验证）。

### 1.3 方案（c）：复用 `colab_request_hook` 已传播的 ephemeral 凭据（`dfs_ephemeral`）

- **机制**：挂载流程中，CLI 的 `drivefs_hook` 已从 Colab 后端拿到一个 ephemeral token（`/tun/m/credentials-propagation/{endpoint}`，`authtype=dfs_ephemeral, version=2`），并经 WebSocket 传给 kernel 供 DriveFS 使用。理论上该 token 也是一个 Google OAuth access token，或可直接用作 Drive v3 API 的 Bearer token。
- **优点**：无需新增授权 ceremony、无需本地 mint、无 token 跨机器传输；凭据由 Colab 后端签发、随挂载会话短暂有效。
- **缺点（决定性）**：
  - **用途未验证**：`dfs_ephemeral` token 是 Colab 内部为 DriveFS 挂载签发的凭据，其 scope、audience、是否允许调用公开 Drive v3 API，在官方文档与 vendored 代码中均无说明。把它从 hook/kernel 中取出并挪作 Drive v3 调用，属于**挪用未公开的内部凭据**，不在 CLI 的设计契约内。
  - **脆弱性**：Colab 后端可随时收紧该 token 的可用范围或改动传播协议；一次 CLI 升级即可让方案失效，且失效时无明确错误语义（可能表现为 403，难以与配额/权限错误区分）。
  - **提取本身有风险**：token 须从 hook 的内存或 kernel 侧取出，提取路径本身就是一个新的泄露面；且 hook 的设计是“回 kernel”，不是“交给用户代码”。
- **结论**：**不推荐用于生产**。仅可作为研究项：一次 smoke test 验证该 token 是否能调用 `files.get`（只读元数据），结果只用于记录，不作为生产依赖。若验证失败，直接放弃，不再投入。

### 1.4 比较总表

| 维度 | (a) VM 自身 ADC | (b) 本地 mint 短期 token ★推荐 | (c) 复用 dfs_ephemeral |
|---|---|---|---|
| 长期 refresh token 是否出境 | 否 | 否 | 否 |
| 交互式浏览器授权 | 需要（GPU 计费时间内） | 不需要 | 需要（挂载 ceremony 的一部分） |
| Scope 可控性 | 不可精确控制（待实测） | 可精确收窄到 `drive.readonly` | 未知（内部凭据） |
| 有效期 | 会话级（待实测） | 约 1 小时，明确 | 未知 |
| 可撤销性 | 会话结束失效 | 过期自动失效 + 账号页可撤销 | 会话结束失效 |
| 失败时诊断清晰度 | 清晰（官方路径） | 清晰（401/403 语义明确） | 不清晰（内部行为） |
| 对 CLI/后端实现的依赖 | 低 | 低（标准 OAuth） | 高（未公开契约） |
| 实施工作量 | 小（已有 `colab auth`） | 中（mint + 安全传输 + 删除验证） | 小但不可验证 |

---

## 2. Token 生命周期（方案 b）

### 2.1 Scope 最小化

- **远程 token 只授予 `https://www.googleapis.com/auth/drive.readonly`**。该 scope 允许 `files.get`（元数据）与 `files.get alt=media`（下载），不允许任何写操作（创建、删除、修改、分享）。
- **为什么不是 `drive.file`**：`drive.file` 只覆盖“由该应用创建或由用户打开过”的文件。Depot 文件由 `drive_depot.py` 经同一 ADC 创建，理论上在覆盖范围内；但历史 VAE 曾以服务端复制方式入 depot，且 manifest 的验证链依赖服务端元数据全量可读，`drive.file` 的可见性边界存在不确定性。用 `drive.readonly` 换取确定性，代价是 token 可读全部 Drive 内容——该代价通过 1 小时有效期 + 用后立即删除 + 仅只读操作来控制（见 §2.5 的取舍记录）。
- **为什么不是完整 `drive` scope**：本地 ADC 本身是完整 `drive` scope（用户 2026-11-10 为专用 OAuth 客户端明确批准），但远程 token 绝不继承完整 scope。完整 scope 允许删除 depot 与全部 Drive 内容，远程下载完全不需要写能力，授予即违反最小权限。
- **Down-scope 的前提**：从完整 scope 的 refresh token 刷新出窄 scope 的 access token，属于 OAuth 的标准下行授权（down-scoping），前提是 refresh token 当初被授予的 scope 包含目标 scope。本地 ADC 的授权包含完整 `drive`，因此 `drive.readonly` 是其子集，理论上可 mint。**但必须实测验证**（§8 检查项 V-01）：mint 后立即做两件事——tokeninfo 确认返回的 scope 确为 `drive.readonly`；一次负向测试，确认该 token 调用 `files.create` 被 403 拒绝。若任一失败，方案 b 不可上线。

### 2.2 Mint 与本地验证（控制器侧，Muse VM）

1. 控制器调用 `google.auth.default(scopes=[drive.readonly])` 后 `creds.refresh()`，得到 access token 与 `expiry`。
2. 立即本地验证（不记录 token 值，只记录结论）：
   - `expiry` 距现在 ≥ 55 分钟，否则丢弃重 mint；
   - tokeninfo 返回的 scope 恰为 `drive.readonly`，无多余 scope；
   - 负向写测试：用该 token 对一个不存在的文件发起 `files.create`，期望 403；若返回 200/其他，立即丢弃 token 并告警（说明 scope 未收窄）。
3. 只有三项全过，才把 token 交给传输步骤。验证结论以结构化字段写入 run 日志（`token_scope_ok: true/false`、`token_expiry_min: <数字>`），**token 值本身永不写入任何日志、文件或报告**。

### 2.3 Token 如何到达 VM（不落入日志、进程列表、shell 历史）

| 通道 | 是否可用 | 原因 |
|---|---|---|
| `colab exec -f` 文件上传（0600） | **可用，首选** | 与 `h3_remote_worker.py`、`task.json`、`model_manifest.json` 同一上传通道；文件在 VM 上以 0600 落盘，内容不经过命令行、不进入 shell 历史、不出现在 `ps` 输出中。上传后控制器立即校验远端文件 mode 为 0600，否则中止。 |
| 经 stdin 管道传入 `colab exec` | 备选 | 内容不进 shell 历史，但会经过 WebSocket 控制通道；须确保控制器与 CLI 均不回显 stdin 内容到日志。实现复杂度高于文件上传，且无落盘审计点。 |
| 环境变量 | **禁用** | 进程环境可被同用户进程经 `/proc/<pid>/environ` 读取；`colab exec` 的代码字符串本身经 WebSocket 传输并可能记入 history/日志。 |
| 命令行参数 | **禁用** | 对所有同机用户可见于 `ps` 输出；且会记入 CLI history。 |
| 写进 worker 代码 / task.json / manifest | **禁用** | 这些文件会被记入 run 日志与报告，且可能随 run 产物归档。 |
| 打印到 stdout/stderr（包括调试输出） | **禁用** | 远端 stdout 经 WebSocket 回传并写入 `run_*_stdout.log`；任何含 token 的输出都会永久留在日志里。 |

- **传输时的额外防护**：token 文件在 VM 上的路径固定为单个专用路径（例如 `/content/muse_h3/.drive_token`，与 `task.json` 同目录，不在 ComfyUI 或输出目录内）；downloader 以只读方式打开，用后立即 `unlink`；控制器在下载结束后执行一次远端 `ls` 检查，确认文件已不存在（§2.5）。
- **代理说明**：token 文件上传走的是 `colab exec` 的控制通道（经 Muse VM 出口代理的 WebSocket），与下载本身无关；下载流量在 VM 内直连 Google API，不经过 Muse VM 的代理（见 §4.1）。

### 2.4 有效期与过期处理

- **有效期**：Google OAuth access token 约 1 小时。控制器 mint 后立即计时；downloader 在每次 Range 请求前检查剩余有效期。
- **过期前阈值**：剩余有效期 < 10 分钟时，downloader 停止发起新 Range 段，保存 `.part` 进度，等待控制器推送新 token；控制器 mint 新 token（重新走 §2.2 验证）并以同一 0600 文件通道覆盖旧文件；downloader 检测到文件 mtime 变化后重新读取并继续。**旧 token 文件被覆盖即失效，不保留多版本**。
- **过期后行为**：若请求返回 401，downloader 立即停止，不再用同一 token 重试（§6 绝不重试项）；等待新 token，最多等待 5 分钟；超时未收到新 token 则放弃下载路径，按 §6 回退挂载路径或中止本跑。
- **时间预算红线**：40GB 在 55 分钟有效窗口内下完，要求持续速率 ≥ 约 12.1 MB/s（§5.4）。速率低于该值时，控制器应在下载开始后 10 分钟内根据实测速率预判是否超时，提前决定继续（中途换 token）还是回退挂载，避免 token 过期才被动发现。

### 2.5 撤销与事后清理

- **正常清理**：全部 4 个文件校验通过后，downloader 删除 token 文件；控制器随后执行远端检查（`test -e` 返回不存在），并把 `token_file_deleted: true` 写入 run 日志；随后才允许进入 ComfyUI 推理阶段。**删除验证失败 = 本跑失败**，即使视频已生成也不得交付，必须停机并告警。
- **异常清理**：任何中途失败（401、403、校验失败、VM 重启前兆、控制器超时）都触发同一清理：控制器在停机前尽力删除远端 token 文件；若会话已不可达，token 仍会在 1 小时内自然过期，且其权限仅为只读。
- **用户侧撤销**：用户可随时在 Google 账号安全页撤销 Muse H3 Desktop 客户端的授权；撤销后本地 ADC 与所有已 mint 的 access token 同时失效。撤销是兜底手段，不替代程序化删除。
- **泄露应急**：一旦发现 token 值出现在任何日志、报告或聊天记录中，立即（1）撤销客户端授权或等待过期；（2）删除含 token 的文件/日志行；（3）在审计日志中记录事件（不含 token 值）。泄露不改变“refresh token 未出境”的结论，但须按事件处理。

---

## 3. HARD RULES（硬规则，实施与审计时逐条核对）

1. **长期 ADC refresh token 永不复制到 Colab VM**。本地 `~/.config/gcloud/application_default_credentials.json` 不得上传、不得以任何形式（文件、环境变量、代码、聊天）传给 VM；`drive_depot.py` 中“never hand the long-lived ADC file to Colab”的注释是硬约束，不是建议。
2. **远程 token 只能是短期 access token**，scope 仅 `drive.readonly`，有效期 ≤ 1 小时；任何时候 VM 上不得存在有效期超过 1 小时的凭据。
3. **任何 token 值永不写入日志、审计文件、run 报告、聊天记录、manifest、sidecar 或错误信息**。错误处理必须脱敏：Google API 异常可能包含请求细节，记录前只保留 HTTP 状态码与错误类型，丢弃 body 与 header。
4. **服务端验证在下载路径下保持强制**：每个文件下载完成后，必须按 manifest 逐项核对——Drive 服务端 `files.get` 元数据（size、md5Checksum）+ 本地全文 SHA-256（与 manifest 的 `sha256` 比对）。三项中任一缺失或不符，该文件不得使用。
5. **校验失败的文件不得进入 ComfyUI**：删除其 `.part` 与目标文件，中止下载路径（§6 回退），不得静默重下、不得标记为可用。
6. **下载只读**：远端 downloader 只调用 `files.get`（元数据 + `alt=media`）；禁止调用 `files.create / update / delete / copy` 及任何写 API；负向测试（§2.2）须证明写调用被拒绝。
7. **Token 文件 0600 且用后必删**：上传后校验 mode，下载结束后验证删除；删除验证失败即本跑失败。
8. **不安装第三方 DFS**：本方案只用官方 Drive v3 API 与 `googleapiclient` / `requests` 标准库，不引入 `colab-dfs-ext` 或任何第三方 Drive 挂载/同步工具。
9. **预算硬停线不变**：165.27 CU 历史基线与 135.27 CU 硬停线不得重置；下载耗时计入会话 CU，控制器须把“已耗 + 下载预估 + 推理预估 + 清理预留”一并纳入硬停判断（见 §5.5）。
10. **失败不掩盖**：任何回退（下载→挂载）必须在 run 报告中以结构化字段记录原因与耗时，不得把回退写成“正常路径”。

---

## 4. 下载机制

### 4.1 网络路径

- 下载在 Colab VM 内发起，直连 `https://www.googleapis.com/drive/v3/files/{id}?alt=media` 与 `https://www.googleapis.com/upload/drive/v3`（仅元数据读，不上传）。**不经过 Muse VM 的出口代理**——Muse VM 的 TLS 拦截代理只影响本地 `drive_depot.py` 的调用（`drive_service()` 已做代理适配），不影响 VM 内流量。
- 远端 downloader 不得设置任何指向 Muse VM 代理的环境变量；若 VM 内存在代理变量，须显式排除 `*.googleapis.com`，或在 downloader 启动时清空代理设置并记录。
- 控制通道（`colab exec` WebSocket）与数据通道（VM→Drive）分离：控制通道的代理截断问题（assign POST 411、1025 字节截断，见审计 P1）不影响下载速率，但影响控制器向 VM 推送新 token 的时效——中途换 token 的 5 分钟等待上限已考虑该因素。

### 4.2 下载原语

- 首选 `googleapiclient.http.MediaIoBaseDownload`（官方 resumable 下载），`chunksize = 8 MiB`（256 KiB 的倍数，与本地 `drive_resumable.py` 的上传分块一致）；或等价的手工 `Range` 实现（`GET` + `Range: bytes=<start>-`，期望 206 + `Content-Range` 校验）。
- 每次下载前先 `files.get(fileId, fields='id,size,md5Checksum,sha256Checksum,trashed')` 取服务端元数据：trashed 为真即硬停；size 与 manifest 不符即硬停；md5 缺失即硬停（manifest 要求服务端 MD5 在位）。
- 下载顺序：**按文件大小降序**（diffusion 20.97GB → text encoder 15.69GB → video VAE 2.81GB → audio VAE 0.61GB）。理由：大文件先行可最大化利用 token 有效窗口；若中途失败，已完成的大文件不再重下（§4.3），小文件回退挂载的代价也最小。
- **串行下载，不并发**：与本地 seed 的 `download_pinned_model()`“No concurrency”规则一致。并发 4 路会放大 429/配额风险并复杂化断点恢复；速率不足时优先考虑换 token/回退，而非并发。

### 4.3 断点续传与 `.part` / sidecar（复用现有模式）

复用本地已验证的两套模式，远端实现时逐项对应：

| 本地已有模式 | 远端对应实现 |
|---|---|
| `download_pinned_model()`：`.part` 流式写 + HTTP Range 续传 + 3 次有界重试 + 连接 30s/读取 120s 超时 + 字节数与 SHA-256 校验，校验失败删 `.part` 并抛错 | 远端 downloader 同款结构，下载源改为 Drive `alt=media`；`.part` 落在 VM 本地盘（`/content/muse_h3/models/<rel>.part`）；Range 逻辑相同（206 校验 `Content-Range` 起始字节与总长；服务器忽略 Range 回 200 时从零重下，不追加；416 时先校验 `.part` 是否已完整，完整则直接认领，不完整则删 `.part` 从零开始） |
| `drive_resumable.py`：sidecar JSON（0600 原子写）保存上传会话 URI 与源文件指纹，跨进程恢复；会话过期（404/410）时与服务端已完成文件对账 | 远端 sidecar 只保存**非敏感**进度：`{file_id, logical_path, expected_size, expected_sha256, downloaded_bytes, chunk_size}`，0600 原子写；**sidecar 内绝不保存 token 或 session URI**（下载无需 resumable session URI，Range 即状态）。sidecar 用于同一会话内 downloader 进程重启后恢复；源文件指纹不符（size/sha 变化）时拒绝复用 |
| `restore_weights()`：`.part` 写完后全文 SHA-256 校验，通过才原子替换为目标文件；已存在且校验通过的目标文件直接复用 | 下载完成后的落盘沿用同一函数逻辑：downloader 把校验通过的文件交给 `restore_weights()` 的复用路径，或直接复用其“`.part` → 校验 → 原子替换”代码段，不另写一套落盘逻辑 |

- **关键语义（必须写进实现注释）**：`.part` 续传只覆盖**同一会话内的瞬时中断**（网络抖动、进程重启、token 中途更换）。**Colab 会话一旦终止，VM 本地盘即销毁，`.part` 与已下载内容全部丢失**，新会话须从零重下。不要把 `.part` 宣传为“跨会话断点续传”。
- **增量 SHA-256**：下载过程中对已写入字节流式计算 SHA-256（与本地 `file_digests` 的 4 MiB 分块方式一致）；完成后比对 manifest `sha256`，同时比对服务端 `md5Checksum`（本地重算 MD5）。任一不符即按 §3 规则 5 处理。

### 4.4 磁盘预检（下载开始前，fail-closed）

1. 远端执行 `df` 检查目标磁盘可用空间：要求 `可用空间 ≥ 40,073,842,159 × 1.15 + ComfyUI 预留约 5GB`，即约 **51GB**。不足即中止本跑，不开始下载。
2. 实际 VM 磁盘大小在本文写作时**未实测确认**（标准 Colab VM 常见约 100GB，但 G4/A100 runtime 在本项目中未量测）；预检逻辑必须以实测 `df` 为准，不得假设。
3. 每下载完一个文件，复核剩余空间；任一文件下载后剩余空间 < 5GB 即告警并暂停后续下载（防止 ComfyUI 安装与输出无空间）。

### 4.5 进度与结构化时间戳

远端 downloader 每完成一个 256 MiB 写入打印一行结构化进度（只含数字，不含 token 与 URL）：

```text
DRIVE_V3_DOWNLOAD_PROGRESS <relative_path> <downloaded_bytes> <expected_size> <elapsed_sec>
```

控制器侧记录以下时间戳（与挂载路径的审计字段对齐，供 run 报告直接引用）：

| 时间戳 | 含义 |
|---|---|
| `download_token_minted_at` | 本地 mint 完成时间 |
| `download_token_validated_at` | scope/过期/负向写测试通过时间 |
| `download_started_at` | 第一个文件第一个 Range 请求发出时间 |
| `download_file_done_at[<path>]` | 每个文件校验通过时间 |
| `download_token_refreshed_at` | 中途换 token（如发生）时间 |
| `download_failed_at` / `download_failed_reason` | 失败时间与原因（只含错误类型与 HTTP 状态码） |
| `download_token_deleted_at` | token 文件删除验证通过时间 |

---

## 5. 成本 / 时间估算

### 5.1 费率与计费前提

- 实测费率：G4 **10.05 CU/小时**，A100 High-Mem **8.88 CU/小时**（审计 §2.1）。
- 计费从 GPU 分配成功开始，到停机确认结束；下载、ComfyUI 安装、推理、挂载等待全部计入同一会话账单。
- 40GB 下载耗时直接折算 CU：`CU = 小时数 × 费率`。以下按持续下载速率分档（40,073,842,159 字节）。

### 5.2 估算表（纯下载时间，未含 ComfyUI 安装与推理）

| 持续速率 | 下载耗时 | G4（10.05 CU/h） | A100-HM（8.88 CU/h） | 单 token（55 分钟窗口）是否够用 |
|---|---|---|---|---|
| 100 MB/s | 约 6.7 分钟 | 约 1.12 CU | 约 0.99 CU | 是 |
| 50 MB/s | 约 13.4 分钟 | 约 2.24 CU | 约 1.98 CU | 是 |
| 20 MB/s | 约 33.4 分钟 | 约 5.59 CU | 约 4.94 CU | 是 |
| 12.1 MB/s | 约 55 分钟 | 约 9.21 CU | 约 8.14 CU | 临界（须中途换 token） |
| 10 MB/s | 约 66.8 分钟 | 约 11.19 CU | 约 9.89 CU | 否（须换 token；G4 单会话 10 CU 上限已超） |
| 2.5 MB/s（Muse VM 下 HF 的实测速率，仅作最坏参照） | 约 4.5 小时 | 约 44.7 CU | 约 39.5 CU | 否，方案不可行 |

- **速率红线**：单 token 55 分钟窗口要求持续速率 ≥ **12.1 MB/s**；低于该值必须中途换 token（可行，但增加一次控制往返与失败面）。
- **预算红线**：单会话预算上限 10 CU。G4 下下载单独耗时超过约 60 分钟（速率 < 11.1 MB/s）即吃掉整个单会话预算，下载路径在经济上已不可行，必须回退或中止。

### 5.3 与挂载路径的比较

| 项目 | 挂载路径（FUSE） | Drive v3 直下路径（本设计） |
|---|---|---|
| 交互授权 ceremony | 需要，耗时计入 GPU 账单；第 6/7 跑因此各耗 1.11 / 0.74 CU | 不需要（token 本地预 mint） |
| 传输速率 | 未实测（FUSE 读速率未知） | 未实测（Colab VM→Drive  intra-Google，理论高于 FUSE，但须实测） |
| 传输耗时（40GB） | 未知（ceremony + FUSE 复制） | 按速率表：50 MB/s 时约 13 分钟 / 约 2.2 CU（G4） |
| 失败面 | 挂载超时（已修 600s，待验证）、DriveFS 错误 | token 过期、429/配额、磁盘不足 |
| 校验 | `restore_weights` SHA-256（不变） | 同左（不变） |
| 回退 | — | 回退挂载路径（§6） |

- **诚实结论**：在速率实测之前，无法断言直下路径比挂载路径更快或更便宜。直下路径的确定收益是**去掉交互 ceremony 与 FUSE 故障面**，不是“保证更快”。若实测速率 < 20 MB/s，其 CU 成本将接近甚至超过挂载路径的历史消耗，此时应优先修挂载路径而非切换。
- **可并行优化（不强制）**：下载与 `ensure_comfy`（git clone + pip install）无依赖，可并行执行以省 wall-clock；当前 `h3_remote_worker.py` 是串行的（先 `ensure_comfy` 再 `restore_weights`），并行化须另行设计与测试，不属于本设计的 v1 范围。

### 5.4 速率不确定性的处理

- Colab VM→Google Drive 为 Google 内网，合理预期高于 Muse VM 经出口代理下 HF 的 2.5 MB/s，但**本项目中无任何一次实测**。估算表中的 50–100 MB/s 档只是规划假设，不得写入 run 报告当作实测。
- 上线前必须有一次**小文件 smoke test**（§8 检查项 V-05）：用同一 token 下载最小的 audio VAE（605MB）并计时，实测速率决定是否继续下载其余 3 个文件；速率 < 12.1 MB/s 时直接回退挂载，不启动大文件下载。

### 5.5 CU 监控修正（配合本设计）

- 控制器在下载开始前须按实测速率（如无实测，按 20 MB/s 保守档）预估下载 CU，并计入“已耗 + 下载预估 + 推理预估 + 清理预留 0.75 CU”，与 135.27 CU 硬停线比较；任一预估使余额逼近硬停线即中止本跑。
- 下载进行中每 5 分钟用“已耗下载时间 × 当前费率”更新已耗估计（费率以会话实际费率为准，不假设）；**不得用“余额未变”推断“未消耗”**——审计已确认计费可能滞后（前两次分配后余额未见变化，第三跑后才扣减）。
- 165.27 CU 历史基线与 135.27 CU 硬停线不得重置；本设计不改动 `h3_budget.py` 的账本结构，只增加“未入账运行时间估计”字段。

---

## 6. 故障模式与回退

### 6.1 故障模式表

| 故障 | 现象 | 处理 | 是否重试 |
|---|---|---|---|
| Token 过期（401） | Range 请求 401 | 停止当前段，等新 token（≤5 分钟），用 `.part` 续传 | 仅在新 token 到达后继续；同一 token 绝不重试 |
| Token scope 不足（403） | `files.get` 403 | 硬停下载路径；检查 mint 的 scope 是否正确；回退挂载 | 否 |
| 下载配额超限（403 `downloadQuotaExceeded`） | 403 且错误原因为配额 | 停止下载路径，回退挂载；**不得换 token 重试**（配额按账号计，与 token 无关） | 否 |
| 文件不存在/已删（404） | `files.get` 404 | 硬停；核对 manifest 与 depot（404 也可能只是权限问题，见 `get_file_evidence` 注释）；不得重下、不得回退挂载掩盖 | 否 |
| 瞬时 5xx / 429 | 429/500/502/503/504 | 有界指数退避，最多 3 次（沿用本地 seed 的 `DIRECT_BACKOFF_SEC=(2,4)` 模式与 `DIRECT_MAX_ATTEMPTS=3`） | 是，有界 |
| 网络中断 / SSL EOF | 连接断开（本地 seed 已多次遇到 `SSLEOFError`） | 用 `.part` + Range 续传；重建 transport；最多 3 次 | 是，有界 |
| Range 不被接受（200 回复或 416） | 服务器忽略 Range / 区间不可满足 | 按 §4.3 处理：200 则从零重下；416 先校验 `.part` 是否已完整 | 仅一次纠正 |
| 校验失败（size/MD5/SHA-256 任一不符） | 下载完成但校验不过 | 删除 `.part` 与目标文件，放弃该文件的下载路径，**回退挂载路径取该文件**（挂载复制同样 SHA-256 校验） | 否（不静默重下） |
| 磁盘不足 | 预检或中途 `df` 不足 | 立即中止，不开始/不继续下载 | 否 |
| 速率过低（< 12.1 MB/s 持续 10 分钟） | 预估超时 | 提前决策：中途换 token 继续，或回退挂载 | —（决策点，不是重试） |
| VM 会话终止（Colab 端） | 会话丢失 | 本地盘销毁，全部进度丢失；控制器按现有流程核查 sessions/余额/账本后，用新 RUN_ID 重开；**新会话须从零重下** | 新跑重下，不是续传 |
| 控制器与 VM 失联 | `colab exec` 超时 | 按现有 watchdog/停机流程处理；token 在 VM 上 1 小时内过期，仅只读 | — |

### 6.2 回退到挂载路径的触发条件

任一成立即回退（回退本身须记录原因与耗时，§3 规则 10）：

1. 本地 ADC 无效，mint 失败；
2. Down-scope 验证失败（scope 不是 `drive.readonly`，或负向写测试未被拒绝）；
3. Smoke test（audio VAE 605MB）实测速率 < 12.1 MB/s；
4. 403 配额类错误；
5. 任一文件校验失败；
6. 中途换 token 超时（5 分钟未收到新 token）；
7. 下载预估 CU 使本跑逼近硬停线。

回退时：控制器先删除 VM 上的 token 文件（尽力而为并记录结果），再执行现有 `drivemount` 流程（600s 远端超时）；回退后 `restore_weights` 的校验逻辑不变。

### 6.3 绝不重试项（写进实现与测试）

- 同一 token 在 401 后重试；
- 403 配额错误后换 token 重试（配额按账号计）；
- 404 后重试或改用其他文件 ID 猜测；
- 校验失败后静默重下同一文件；
- 磁盘不足后继续下载；
- 整个 4 文件下载在同一会话内失败两次后第三次重下（上限：同一会话内下载路径最多启动两次，第二次失败即回退挂载或中止本跑）。

---

## 7. 安全验证清单

每一项都须有可检查的证据（命令、断言或日志字段），不能只写“已检查”。实施后逐项打勾，任一项失败即本设计不上线。

### V-01 Down-scope 实测

- [ ] 本地 mint 的 token 经 tokeninfo 验证，scope 恰为 `drive.readonly`，无 `drive` 完整 scope、无其他 scope。
- [ ] 负向写测试：用该 token 调 `files.create`，期望 403；返回非 403 即失败。
- [ ] 检查证据：日志含 `token_scope_ok: true` 与 `token_expiry_min: ≥55`，不含 token 值。

### V-02 长期凭据未出境

- [ ] 本地 ADC 文件在 mint 前后 mtime 与 mode（0600）不变；无任何上传该文件的命令或代码路径（grep 控制器与 worker 代码，确认无 `application_default_credentials.json` 的读取后外传）。
- [ ] 远端 VM 上不存在 ADC 文件（`ls` 检查目标路径不存在）。
- [ ] 远端只存在短期 token 文件，且为 0600。

### V-03 Token 传输通道

- [ ] Token 只经 `colab exec -f` 文件上传通道进入 VM；代码中无“环境变量传 token、无命令行参数传 token、无 token 写进 task.json/manifest/worker 代码”的路径（grep `os.environ` 与 `exec` 构造代码确认）。
- [ ] 远端 token 文件 mode 为 0600（上传后立即校验）。
- [ ] 传输与下载全程无 token 值进入任何日志：对全部 run 日志、审计 JSONL、run 报告做 token 模式 grep（Bearer 值、长 base64 串），零命中。

### V-04 Token 删除验证

- [ ] 下载全部完成后，远端 `test -e <token 文件>` 返回不存在；日志含 `token_file_deleted: true` 与 `download_token_deleted_at` 时间戳。
- [ ] 异常路径同样验证：模拟一次中途失败，确认停机流程仍删除 token 文件（或记录“会话不可达，token 将在 1 小时内过期”）。
- [ ] 删除验证失败即本跑失败（即使视频已生成也不得交付）。

### V-05 速率与磁盘 smoke test

- [ ] 用同一 token 先下载 audio VAE（605MB）并计时，实测速率记录在案；速率 < 12.1 MB/s 时不启动其余 3 个文件，直接回退挂载。
- [ ] 下载前 `df` 预检通过（可用 ≥ 约 51GB），检查结论写入 run 日志。
- [ ] 实测速率写入 run 报告（替换估算表中的假设值，不得把假设当实测）。

### V-06 校验链不变

- [ ] 每个文件下载后三项全过：服务端 `files.get` size 与 manifest 一致、服务端 MD5 与 manifest `server_md5` 一致、本地全文 SHA-256 与 manifest `sha256` 一致。
- [ ] 校验失败时 `.part` 与目标文件被删除，且 run 日志记录回退原因（不含 token）。
- [ ] `restore_weights` 的复用路径（已存在且校验通过则跳过）行为不变；下载路径产出的文件能被 `restore_weights` 正常认领。

### V-07 故障注入（Mock / 演练，实施阶段执行）

- [ ] 模拟 token 过期（401）：downloader 停止等待新 token，不重试旧 token；新 token 到达后续传成功。
- [ ] 模拟 403 配额：downloader 停止，不换 token 重试，正确回退挂载。
- [ ] 模拟校验失败：文件被删除并回退，不静默重下。
- [ ] 模拟中途断网：`.part` + Range 续传成功，增量 SHA-256 连续正确。
- [ ] 模拟磁盘不足：预检拒绝下载，不开始传输。

### V-08 预算与账本

- [ ] 账本基线 165.27 CU 与硬停线 135.27 CU 未被重置（跑前跑后各核对一次）。
- [ ] 下载 CU 预估（含未入账运行时间）写入 run 报告；实际消耗在停机后与余额变化对账，差异注明“计费可能滞后”。

### V-09 第三方依赖

- [ ] 远端 downloader 只用标准库 + `googleapiclient`/`google-auth`/`requests`（Colab VM 预装或 pip 安装的官方包）；未引入 `colab-dfs-ext` 或任何第三方 Drive 工具。
- [ ] 不改动 depot、不调用 Drive 写 API（审计 run 日志中写 API 调用次数为零）。

---

## 8. 实施前提与阻塞项

### 8.1 阻塞项（任一未解决，设计不得进入实施）

1. **本地 ADC 有效性未知**。ADC refresh token 为 Testing 模式，2026-11-10 授权时已提示约 7 天过期风险；截至本文写作时未重新验证。mint 依赖 ADC 有效——实施前必须先跑 `account-check`（只读）确认 ADC 仍有效；若已过期，须用户重新授权后才能 mint，**不得绕过**。
2. **Down-scope 未实测**。从完整 `drive` scope 的 refresh token 能否 mint 出仅 `drive.readonly` 的 access token，且写调用确实被 403 拒绝，须以 V-01 的实测为准；若 Google 拒绝下行收窄，方案 b 须改设计（例如改用独立的只读 OAuth 客户端，须用户另行批准创建）。
3. **Colab VM→Drive 速率与磁盘未实测**。速率决定单 token 是否够用、下载是否经济；磁盘决定能否装下 40GB。V-05 的 smoke test 是实施的第一步，不是可选项。
4. **实施本身须用户另行批准**。本文仅为设计；新 downloader 模块、控制器改动、Mock 之外的任何实测（含 smoke test）都须按用户指令另行批准后才能做。

### 8.2 非阻塞但须记录的风险

- `dfs_ephemeral` token 的 Drive v3 可用性未验证——只影响方案（c）的评估，不影响推荐方案（b）。
- VM 内 `googleapiclient`/`google-auth` 的可用版本未核对——Colab VM 通常预装，但实施时须在 downloader 启动时做 import 检查，缺失则 `pip install`（须计入下载前耗时与 CU）。
- 中途换 token 的控制往返受 Muse VM 出口代理影响（P1 类截断只影响控制通道，不影响下载本身）；5 分钟等待上限已考虑该因素，若实测控制往返经常超时，应收紧为“速率不足即直接回退挂载，不中途换 token”。
- 下载与 ComfyUI 安装的并行化未设计——v1 保持串行，CU 成本按串行估算；并行是后续优化，不作为上线条件。

### 8.3 与挂载修复的关系

- 本设计**不替代**正在进行的 600s 挂载修复：vendored CLI 已含 `timeout_ms=600000` + 660s 本地 ceiling，该修复仍是主路径；直下路径是备选，两条路径共存，控制器按 §6.2 的条件选择。
- 若 600s 挂载修复在下一跑验证通过且授权 ceremony 稳定在 2 分钟内完成，挂载路径的 CU 成本将低于直下路径在低速率下的成本；此时直下路径可降级为“挂载失败后的第二选择”，不强制切换。

---

## 9. 结论

- **推荐认证方案**：(b) 本地 mint 短期 `drive.readonly` access token（约 1 小时），经 `colab exec -f` 以 0600 文件传给 VM，用后立即删除并验证删除。长期 refresh token 永不离境；交互授权 ceremony 不再占用 GPU 计费时间。
- **最大不确定性**：Colab VM→Drive 的实际下载速率（未实测）。速率 ≥ 20 MB/s 时直下路径约 5.6 CU（G4）以内完成 40GB，经济可行；速率 < 12.1 MB/s 时单 token 不够用，须中途换 token 或回退挂载；速率接近 Muse VM 下 HF 的 2.5 MB/s 时方案整体不可行。
- **上线门槛**：V-01（down-scope 与负向写测试）、V-02（长期凭据未出境）、V-05（605MB smoke test 速率与磁盘预检）三项必须先通过；任一失败即不上线，回退挂载路径。
- **安全底线**：校验标准不变（服务端 size + MD5 + 本地全文 SHA-256），token 永不入日志，删除验证失败即本跑失败，预算基线与硬停线不变。

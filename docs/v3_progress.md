# v3 进度记录（工作笔记，非最终报告）

- 2026-11-10 02:2x UTC：v3 包解压到 ~/workspace/Muse_Colab_H3_Drive_v3/（36 文件），
  与 P0 逐文件 diff 完成：新增 auth_support.py、build_api_candidate.py、
  h3_drive_probe.py、tests/test_auth_and_transport.py；改动 drive_depot.py、
  h3_controller.py、prompt/README/DEPLOYMENT_STATUS；其余文件与 P0 相同。
  本轮对包内源码零修改；P0 目录未动。
- venv：Python 3.12.3，google-colab-cli==0.7.5，google-auth 2.62.1，
  google-api-python-client 2.201.0，huggingface-hub 1.33.0。离线测试 36/36 通过。
  plan 正常。海报 SHA-256 与合同一致；海报 MD5 与 Drive 插件侧一致
  （33f99bdfa5deca5607364737ab9521eb）。
- gcloud：官方 tarball 装于 ~/google-cloud-sdk/google-cloud-sdk/，版本 592.0.0。
- Drive 插件：已连接，账号 [GOOGLE_ACCOUNT_REDACTED]；海报与剧本两个 File ID
  均可读（属主 [GOOGLE_ACCOUNT_REDACTED]，共享可见）；Drive 配额上限约 5TB，
  已用约 3.4GB。本地 storyboard.md（1332 字节）是 Drive 完整剧本（3305 字节）
  的摘录版，内容主线一致。
- HF 已 pin：revision 54472221d27c695fca9621b0c3ed52f06c4af707（40 位）。
  四文件合计 40,073,842,159 字节 = 37.32 GiB；license: other。
  LFS SHA-256 已记录（见最终报告）。home 剩余 138GB，1.3 倍 staging 空间够。
- COMFY_REF（git ls-remote HEAD）：61e524bdba7a4e193b45c2697f1af83c1beb86d6。
- 官方 H3 模板已下载（29 节点/33 链接），API 候选已生成（26 节点），
  静态契约检查 API_STATIC_CONTRACT_OK（不等同真机 /object_info 验收）。
- 环境注意：VM 默认 NO_PROXY 含 [::1] 等方括号条目，会使 huggingface_hub
  的 httpx 抛 InvalidURL。HF 相关命令须用干净 NO_PROXY：
  NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1。
  seed 时同样要带这组变量。
- 待用户（阶段 1 门禁）：
  1. 在 Cloud Console 建自己的 Desktop OAuth 客户端（启用 Drive API、
     同意屏幕/测试用户、Drive restricted scope），把客户端 JSON 以文件附件发来，
     我会放到 $HOME/private/desktop_oauth_client.json（目录 0700、文件 0600）。
  2. 在交互终端执行 gcloud ADC 授权命令（见最终报告/聊天），授权码只贴回原终端。
     授权账号必须是 [GOOGLE_ACCOUNT_REDACTED]（与 Drive 插件同一账号），
     否则 account-check 会失败。
- 未做的：未发起任何 OAuth/ADC 授权，未创建 GPU，未消耗 CU，未上传/下载模型。
- 2026-11-10 04:05 UTC：浏览器任务完成。新建项目 muse-h3-colab（编号 493117617570），
  Drive API 已启用，同意屏幕 External/Testing、测试用户 [GOOGLE_ACCOUNT_REDACTED]、
  六个 scope 已保存、受限 Drive scope 未被拒；Desktop 客户端 Muse H3 Desktop 已建。
  客户端 JSON 已核对（client_id 与报告一致、project muse-h3-colab）并移入
  $HOME/private/desktop_oauth_client.json（目录 0700、文件 0600），浏览器下载副本已删。
- 2026-11-10 约 04:05 UTC：ADC 授权尝试已停止。gcloud 592.0.0 提示 --no-launch-browser
  不再与 --client-id-file 联用，改用 --no-browser 后，后台终端流程的输出未通过安全检查，
  按系统要求未采用该输出、已终止该进程。ADC 仍未授权，account-check 未跑。
  下一步等用户决定授权方式。
- 2026-11-10 04:18–04:24 UTC：用户决定自己在浏览器完成 Google 授权并把结果发来。
  我给的授权链接是仅用可信输入拼的（client_id 来自浏览器任务报告、六个 scope 来自
  v3 prompt、redirect_uri=http://localhost，未用被标记的终端输出里的任何内容）。
  用户发回 localhost 重定向 URL（含授权码），我用一次性脚本 /tmp/exchange_adc.py
  （读码只走 stdin、不打印、不存盘）当场换成 ADC，0600 写好；脚本已删。
  阶段 1 门禁全部通过：
  - ADC audit：present、mode 0600、credentials 未读取。
  - colab --auth=adc version 0.7.5；usage：余额 165.27 CU（与历史基线一致，
    硬停线 153.82 → 剩余 11.45 CU）、费率 0.00/hr、活跃会话 0。
  - sessions：无活跃会话。whoami：[GOOGLE_ACCOUNT_REDACTED]，
    Audience 为本客户端 ID，七项 scope 齐全，token 59 分钟有效。
  - Drive about.get（API 直连）：[GOOGLE_ACCOUNT_REDACTED]。
  - account-check：{"same_google_account": true, "no_gpu_created": true}。
  同意屏幕 External/Testing，refresh token 可能 7 天过期，须实测刷新行为，不承诺永久。
- Muse VM 传输适配（2026-11-10，均为最小增量补丁，已记入最终报告，包内其余代码未动）：
  1. venv 装 PySocks：无它时 httplib2 的 isgood() 恒假，代理恒被绕过，
     直连被透明拦截 → SSL WRONG_VERSION_NUMBER。
  2. drive_depot.drive_service()：显式用环境代理 + SSL_CERT_FILE（出口拦截 CA）
     构造 httplib2.Http，再包 AuthorizedHttp 传给 build()；否则 Drive API
     报 CERTIFICATE_VERIFY_FAILED。已验证 about.get 正常。
  3. drive_depot.list_named()：page_size → pageSize（v2/v3 共有的真 bug，
     Drive v3 API 只认 pageSize，Mock 测试测不出来；init 因此 TypeError 失败）。
  4. drive_resumable：出口 MITM 代理会剥掉空 PUT 的 Content-Length: 0，
     状态查询恒回 411（requests/httplib2/curl 三者同证）。改为：查询只作参考，
     411/异常时回退 offset 0、由下一块 PUT 的 308 Range 纠正偏移；
     末尾改用重 PUT 最后一块来确认完成（均已实测：重 PUT 已存范围幂等，
     完成后再 PUT 返回 200 同一 file id）。新增 2 条测试，套件 38/38 通过。
- 2026-11-10 04:2x UTC：init 成功，Muse-H3-Model-Depot 已建，
  root_id 1YmCfFOOsoMvREhdyHusBDy95C1eI4KE_。
  smoke 成功：canary 30 字节上传 + 服务端 MD5 校验通过，
  file_id 1C8-Je_m9YPeHDpcZmEANiQOQpd25C5s-。
  调试文件已清（dbg_complete.bin 已删，204）。
- 2026-11-10 04:2x UTC：seed 已在后台启动（session proc_b4a6d1ba4736），
  四文件 37.32 GiB：HF 下载 + Drive 上传，干净 NO_PROXY。完成后来 handoff
  再跑 verify。CU 消耗仍为 0。
- 2026-11-10 04:29–04:32 UTC：seed 被批准弹窗拦下后由用户下令暂停。
  审批记录：同一 seed 命令 04:29:25–04:29:41 内产生 20 张以上批准请求，
  全部 Denied，约每秒一张；当前挂起 0 张。seed 进程已终止（SIGTERM），
  0 字节模型已下载，0 字节已上传。
- 2026-11-10 04:44 UTC：用户下令只恢复 Drive 模型仓库阶段，不启动 G4；
  保留已完成的 ADC/同账号/depot root/smoke/HF pin/静态工作流，不得重授权
  或重建仓库；新增 --source direct。
- 2026-11-10 04:47 UTC（小文件验证，通过，0 张新批准卡）：
  - 前台 HF 请求不触发批准弹窗：fetch_pinned_metadata（1 次 HF API 调用）
    与 direct 小文件下载各在约 2 秒 / 约 4 秒内完成，审批记录无新增。
    此前弹窗风暴是后台 exec 待批准时约每秒重发一张卡所致，与 HF 请求数无关。
  - pin 已写入 model_cache/pinned_hf_files.json（revision 54472221...，
    四文件大小与 LFS SHA-256 与此前记录一致）。
  - 小文件探针：embeddings/minimaxh3_art_is_explosion.safetensors（512,120 B）
    经 download_pinned_model 下载，DIRECT_DOWNLOAD_OK（大小+SHA-256 均对上）。
- 2026-11-10 04:51–04:55 UTC（Drive 断点续传实测，通过）：
  - 确定性 128 MiB 临时文件（本地 MD5 fe609f0e4a6a6f16...）两次上传均服务端验证通过
    （大小 134217728、MD5 一致、parent 一致）。
  - A 上传中注入一次网络断点，仍完成（重试/续传路径被实际走通）。
  - 服务重启中途杀死进程后，B 凭 sidecar 跨进程恢复：首个 chunk PUT 回
    308 Range bytes=0-16777215（已传 16MB 被跳过），随后补完并验证通过。
  - 临时文件已全部删除（Drive 两个文件 + _tests 文件夹 + 本地 _tests/_probe）。
- 2026-11-10 09:59 UTC：seed 退出码 2，stderr `BLOCKED: SSLEOFError`。
  核查：第 1 个文件已在 Drive 上（id 1Dwe-Kp7xgeGGtv2DJWeHQSH8l_m77W5k，
  20,970,379,616 B，服务器 MD5 9747caca7468a226d12f4dc098f4db39），上传会话
  09:55 已正常结束（sidecar 已删），只是验证/写状态那步被 SSL 错误打断，
  depot 状态仍 UPLOADING、verified_files 空。Drive API 随后实测正常，
  属偶发连接失败。本地重算 21GB 文件：MD5 与服务器全值一致，
  SHA-256 与官方 LFS 值全值一致（9255f52b…5779）。
- 2026-11-10 10:06 UTC：同一命令第三次重跑（新会话 proc_9399642507c2），
  认出 Drive 上已有的第 1 个文件、验证后记入状态，再继续第 2、3、4 个文件。
  无需重下、无需重传第 1 个文件。
- 2026-11-10 10:29 UTC：第 1 个文件正式验证通过，1/4。depot 状态 VERIFYING，
  verified_files 有 1 条：drive_file_id 1Dwe-Kp7xgeGGtv2DJWeHQSH8l_m77W5k，
  parent 1axLvpwFRPaj9LdCbOJmckLag7Jmsa98p，server_md5
  9747caca7468a226d12f4dc098f4db39，server_verified true，
  sha256 9255f52b…5779，size 20970379616。第 2 个文件（text encoder）
  已开始下载，10:31 时 .part 760,217,600 B（约 4.8%）。
- 2026-11-10 15:43 UTC：第 2 个文件正式验证通过，2/4。depot 状态 VERIFYING，
  verified_files 有 2 条：第 1 个（id 1Dwe…，server_verified true）、
  第 2 个（id 1AcJ7E4Cj1ZMJEvDbQc9RwbOluvHGu5Ik，parent
  15C9xdIG5bisZP51AA8MtYa1aOOdiBj4s，server_md5 b0412eae26c7fb95e77139dc0f03c8b8，
  server_verified true，sha256 35a88d51…f2c6，size 15687142551）。
- 2026-11-10 18:14 UTC：第 3 个文件正式验证通过，3/4。depot 状态 VERIFYING，
  verified_files 有 3 条：第 1 个（id 1Dwe…）、第 2 个（id 1AcJ…）、
  第 3 个（id 1qD0UB8KnSX1YK63-N5iAORn-bJa9ocbr，parent
  1W5ebIxgqSfxFXTnoJag2GROeij2-eD8F，server_md5 6360ffe68680322ddcd0c0d0274d63c3，
  server_verified true，sha256 8e505d95…ae4db48，size 605254808）。
  第 3 个文件是经 LEGACY_VAE 服务器端复制完成的（包内设计：历史 Drive 文件
  1NMhFPyq7qVSmZWJZg506LArud39lMTCF，经大小/MD5/SHA 核对后服务端复制，
  再验证——不是绕过校验）。
- 2026-11-10 22:36 UTC：CACHE_READY。seed 第十七次运行成功，退出码 0。
  1、2、3 号文件 FILE_VERIFIED；第 4 个文件先打出
  LEGACY_COPY_UNAVAILABLE_FALLBACK_UPLOAD（SSLEOFError），
  回落上传途中又一次 TRANSIENT_DRIVE_ERROR_RETRY（attempt 1），
  随后 FILE_VERIFIED（2,811,065,184 B）。seed 输出
  {"ready": true, "files": 4, "bytes": 40073842159}。
  随后 verify --colab-auth adc 通过：
  {"ready": true, "files": 4, "bytes": 40073842159,
   "account_verified": true, "server_metadata_rechecked": true}。
  四文件 Drive 侧复核：diffusion 1Dwe-Kp7xgeGGtv2DJWeHQSH8l_m77W5k
  （20,970,379,616）、text encoder 1AcJ7E4Cj1ZMJEvDbQc9RwbOluvHGu5Ik
  （15,687,142,551）、audio VAE 1qD0UB8KnSX1YK63-N5iAORn-bJa9ocbr
  （605,254,808）、video VAE 1pqQNy8dky237QFlpt4wpsii0N5j7UtNF
  （2,811,065,184，MD5 1522fc49e094bb75c704ee519582252d，
  父文件夹 vae 1W5ebIxgqSfxFXTnoJag2GROeij2-eD8F，已实地确认）。
  READY manifest 在 Drive 根目录：
  READY_54472221d27c695fca9621b0c3ed52f06c4af707.json，
  id 1jLYefARIDs8VLtsCXJPuTU7zHXw0coly，2,174 B，
  MD5 38308c4c6ec8c72dae76fc8653c8a78b，
  父文件夹即 depot 根 1YmCfFOOsoMvREhdyHusBDy95C1eI4KE_，已实地确认。
  本轮 depot 阶段完成。G4 仍未启动，CU 消耗仍为 0。
  剩余 G4 前置：挂载 PTY 程序已离线验证、未端到端实测；
  live /object_info 未做；ADC refresh token 为 Testing 模式，
  约 7 天过期风险（2026-11-10 授权）。

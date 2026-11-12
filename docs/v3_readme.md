# v3 修订说明 — Muse → Colab CLI → Drive Model Depot → MiniMax-H3

## 用户已授权的操作范围

- 使用 Muse Linux 中的**用户私人**持久 ADC 凭据访问 Colab 与完整 Drive；由用户在 Google 浏览器/交互终端完成授权码输入，Muse 不读取秘密。
- Drive 插件已连接，但它的连接不自动传递给本地 Python SDK；需实际比对用户身份。
- 完成同账号 Drive 模型仓库（包含真实四权重、版本化 READY JSON），包括真实大文件上传。
- 当实时配额与技术门禁满足时尝试一次 A/B 视频：项目内最多 10 CU 的单任务软件预算（历史剩余额度为 11.45CU），不自动购买 CU，失败优先停止 G4。

## 相对 v2 包的具体修改

1. 新增 `auth_support.py`：支持 `COLAB_CLI_PATH`、当前 Python venv、项目 `.venv/bin/colab`、PATH；凭据权限仅检查 metadata，不打开文件内容。
2. `drive_depot.py`：用户明确批准的 Drive 全范围 scope；`init` 防止断电后的重复目录；`smoke` 的 Drive v3 真实小文件验证；对于历史 VAE，**只在本地 HF 真正内容散列匹配时**才复用服务端 `files.copy`；对固定 HF revision 四文件做 308/Range 持久上传。四文件都通过后把版本化 `READY_<revision>.json` 上传 Google Drive，经服务端 MD5/size/parent 验证再发布本地 READY；后续 `verify` 和 G4 前在线再核验。
3. `h3_controller.py`：修复 venv CLI 解析；移除未授权第三方 DFS 依赖；G4 前检查官方 `colab drivemount --help`、交互 TTY 和授权参数，付费会话内用官方 `drivemount`。新增 mounted Drive 四模型可见性预检，早于 ComfyUI pip/模型加载。
4. 新增 `h3_drive_probe.py`：挂载后核对四模型是否能从 `/content/drive/MyDrive/Muse-H3-Model-Depot` 被看到、字节数是否正确；NVMe 阶段依然强制逐文件 SHA。
5. 新增 `build_api_candidate.py`：官方 UI JSON → 仅供下一步验证的 ComfyUI API 候选图；不能把此候选当作真实 GPU 工作流验收。
6. 新增 `tests/test_auth_and_transport.py`：CLI 查找、ADC 权限、官方 Drive mount、root 幂等、smoke 门禁、历史 VAE 散列、防假 READY 等离线测试。

## 本地离线测试

```bash
python3 -m py_compile *.py
python3 -m unittest discover -s tests -q
python3 h3_controller.py plan
```

测试未覆盖：真实 Colab OAuth/ADC、Drive 插件身份、Google 37GB 上传、官方 Drive mount 交互、ComfyUI /object_info、G4 申请、A/B 推理。注意 Muse VM 仍可能被替换，`finally` 不能覆盖外部强制终止；临时会话需持久记录并定期独立检查。

## 重要安全事实

- `https://www.googleapis.com/auth/drive` 属于 Google Drive Restricted Scope。用户同意不等于 OAuth app 一定通过 Google 的授权/验证；External/Testing 状态用户 refresh token 可能在 7 天失效。
- 统一 ADC 默认位于 `~/.config/gcloud/application_default_credentials.json`，包括敏感 refresh token，用户已同意保存在 Muse，但必须 0600 且不得提交、打印或上传 Colab。
- `h3_controller` 的 `--budget-cu` 是**软件限额**，不等同 Google 计费硬封顶。历史硬停余额 `153.82 CU`、历史项目剩余 `11.45 CU`，每次真实执行仍以实时 `usage` 为准。
- 目前控制器的官方 `colab drivemount` 需交互式 shell；若 Muse Agent 工具不提供 TTY，流程将在 GPU 申请**之前**阻断，不能谎称无人值守全自动。后续若需真正无人值守，应单独实现并评审安全的远程 Drive 读取通道。

详见 `MUSE_EXECUTE_AUTH_DEPOT_VIDEO.md`。

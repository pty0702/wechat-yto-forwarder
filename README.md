# 微信 ⇄ 圆通客户管家消息转发

Python 桌面工具：按运单号和关键词处理微信群文本，将请求转到客户管家，再将机器人的相关回复搬回微信。

## 当前可用内容

- 中文配置窗口，启动、停止、实时日志和独立演示。
- YT 运单筛选、屏蔽词、@客服分流、完整机器人昵称匹配、30 秒静默超时。
- 消息事件去重、发送失败缓存、发送结果不确定时停止，避免盲目重发。
- Windows UI Automation 适配器、窗口诊断、启动历史基线、目标群与进程锁定、发送回显检查。
- 默认关闭的实验性微信兼容选项：仅对已核对的固定构建临时激活无障碍控件。

**真实双向收发尚未完成本机验收。** 2026-10-23 已在本机微信 `4.1.15.56` 的固定构建上，通过界面诊断临时激活控件，读到目标群标题、`chat_input_field` 和 `chat_message_list`，并在诊断结束后恢复原状态 `0`。这证明了控件激活与恢复路径，不代表真实收发已经可用，也不代表支持任意微信 4.x。

当前仍有两处阻碍：微信文本消息是全宽行，没有发送者子控件，尚不能可靠排除本人消息及回传自环；客户管家 `2.0.0.2` 输入区域的 `ValuePattern` 不可用，`TextPattern` 返回对象占位符，启动检查会明确拒绝这种无法读取完整草稿的状态。仓库中的 `uia.example.json` 仍只是格式示例。

实验性兼容会临时修改已核对微信进程的无障碍状态字节，不修改客户端磁盘文件，不读取聊天数据库。正常结束、停止及可控异常会执行恢复；强制结束或崩溃时无法保证恢复，需重启微信。兼容功能另外要求 64 位 Windows 和 64 位 Python。

## 启动

需要 Windows、Python 3.9+（含 Tkinter）和 PowerShell 7。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\python版\requirements.txt
.\.venv\Scripts\python.exe -X utf8 .\python版\gui.py
```

已有虚拟环境时，也可使用项目内的 `启动.ps1`。

界面默认选择演示模式，点击“开始运行”可查看约 12 秒的内置案例。演示只在内存处理样例，不需要真实群，也不会保存样例配置。

点击“窗口诊断 (Ctrl+D)”或按 `Ctrl+D` 导出当前控件。需要验证上述固定构建时，勾选默认关闭的“微信4兼容（实验性，停止后恢复）”再诊断；诊断期间可点击“停止”，等待日志确认恢复完成。完整步骤见下方 Python 版说明。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s .\python版\tests -v
.\.venv\Scripts\python.exe -X utf8 .\python版\wechat_forwarder.py
```

当前 132 项自动化测试通过；测试覆盖业务规则、模拟控件、进程锁定、发送确认和兼容会话恢复，不替代真实客户端收发验收。

详细业务规则、适配配置格式、已知限制与诊断说明见 [Python 版说明](python版/README.md)。

本地 `config.json`、`profiles/local*.json`、`diagnostics/`、虚拟环境和缓存不提交到 Git。

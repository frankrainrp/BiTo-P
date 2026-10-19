# FamilyOS 公开工程展示版

FamilyOS 是一个面向家庭场景的本地优先协调系统，将任务、日历、习惯、个人活动摘要与可选的 AI 助手连接起来。

这份仓库供招聘与技术评审使用，包含可运行后端、24 份数据库迁移、自动化测试和使用虚构数据的演示。生产界面素材、真实家庭数据、模型权重与私人部署配置不在公开版中。

## 建议阅读顺序

1. 运行 `python examples/demo.py`：通过真实 HTTP 接口验证两个成员的任务可见性、任务完成和未登录请求拒绝。
2. 阅读 [工程说明](ENGINEERING.md)：理解模块化单体、SQLite、权限边界和后台任务的取舍。
3. 阅读 `familyos/companion_actions.py`：模型只能提出操作建议，业务代码在用户确认后执行。
4. 阅读 `tests/test_http.py`、`tests/test_agent_jobs.py`：查看可复现的接口与生命周期测试。

Python 3.12+ 环境安装 `requirements-dev.txt` 后即可运行演示与 `python -m unittest discover -s tests -v`。Docker 启动方法见主 README；不依赖家用服务器、GPU 或模型 API Key。

## 工程重点

- 成员与家庭范围内的数据访问，私有/共享任务可见性。
- 将不可信的模型输出转换为受约束、可确认的操作。
- 本地处理屏幕活动，服务端拒绝原始截图字段。
- 定时任务、取消、去重、通知目标和监控冷却控制。
- 通过 GitHub Actions 验证测试、Compose 与隔离容器健康。

当前用户名登录属于可信本地环境模式，不能直接用于公网身份验证。公开源码不等于公开运行私人服务；本仓库没有通往私人服务器的自动发布连接。

## 下载

[Windows API 便携包及源码包](https://github.com/frankrainrp/FamilyOS-Showcase/releases/tag/v0.1.0)：便携包已带 Python，解压后运行 `Run-Demo.cmd` 或 `Start-Server.cmd`。公开版不含生产网页 UI。[组成与端口](DISTRIBUTION.md)；[待制作的两类图](DIAGRAM_BRIEF.md)。

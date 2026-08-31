# 发布与部署

LifeOS 以一个 Python distribution 和同版本的 `skills/lifeos/` 交付。唯一版本源是 `lifeos.py` 的 `VERSION`；Git、Tag、制品、本机 pipx、Skill Runtime 和真实个人 Runtime 是独立状态。

## 候选

1. 从同一已提交版本运行 README 的“开发与验证”、`python3 -m build` 和 `python3 lifeos.py --version`。
2. 测试只使用合成 fixture 与临时 Runtime；核对制品不含个人 Runtime、私有配置、来源标识或用户偏好。
3. 在临时 pipx 环境安装候选制品，核对版本、帮助和最小无副作用入口。

## 发布与同步

正式 `vX.Y.Z` Tag 只指向完成候选核验的 Commit；Push 和 Tag 分别授权。新增制品渠道前先确认消费者、上传入口和回滚方式，本 Repo 不假定 PyPI 或自动发布。

分别运行 `scripts/sync-to-smartwork.sh` 和 `scripts/sync-to-ccswitch.sh`，将仓库中的完整 LifeOS Skill 同步到 SmartWork 和 cc-switch，并删除目标中的漂移文件。源码验证、CLI 安装和 Skill 同步不能互相证明。

## 恢复

- CLI 恢复上一已验证 Tag 对应的完整 distribution，不替换单个模块。
- Skill 恢复上一完整 `skills/lifeos/` 目录，再核对宿主发现。
- 真实个人 Runtime 不随代码或 Skill 自动降级；Schema、配置或数据恢复需要独立备份、兼容判断和授权。

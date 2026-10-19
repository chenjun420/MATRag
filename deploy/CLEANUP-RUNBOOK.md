# 板卡部署前置清理闸门（ATERag → MATRag）

> **硬性规则**：MATRag 生产部署到板卡之前，必须先完成本清单并全部打勾。
> 未完成清理，不得执行任何 MATRag prod 部署动作（`deploy/native/*`、pgvector 建库、explorer 等）。
>
> 动机：ATERag 与 MATRag 是同一职责的先后两代。双份服务并存会造成
> ①端口/库名混淆 ②"哪份服务在应答"不可知 ③ATEStudio 侧引用漂移。
> 先清后建，单一事实来源。
>
> **执行状态（2026-10-16）**：已在 192.168.5.25 全量执行（用户裁定不备份直接删）；.24 已不可达，并于当日从资产清单与 SSH 配置中全面移除（不再纳入任何操作）。
> 执行记录见 `deploy/RECORDS-20261016.md`；本文档保留作流程参考。

## 资产盘点范围（两台板卡）

| 主机 | 角色 | 需清理的 ATERag 资产 |
|---|---|---|
| 192.168.5.25 | 存储（PG17） | PG 库 `power_specs`（+ vector/age/pg_textsearch/zhparser 扩展属库级，随库删除）、`/opt/aterag` 数据目录、`/var/log/aterag` 日志 |
| ~~192.168.5.24~~ | **已移除（2026-10-16）** | 整机不可达；永不纳入任何操作（SSH 别名已从 Windows/WSL 配置删除） |

> 具体以盘活结果为准（`scripts/deploy_inventory.sh`）。上表是已知范围，不是假设范围。

## 闸门步骤

### Step 0 前置确认（人工，缺一不可）
- [ ] MATRag prod profile（PG + explorer）已在本仓库完成自验（不是"计划完成"）
- [ ] **ATEStudio 对 ATERag 的依赖已确认处置**：ATEStudio 的`models_catalog`（只读 MCP 取型号目录）、`/aterag` 导入向导、`/aterag-review` 评审流程目前读的是 aterag-mcp（实际部署在 .25:8080，已随清理移除；MATRag 对接见 TODO P4-1）。停掉它这些功能会掉线——必须先确认切换目标（指向 MATRag gateway）或接受暂不可用，**不许悄悄掉**。
- [ ] `power_specs` 数据去留已决定（见 Step 3，默认：dump 备份后删除）

### Step 1 盘点（只读，可随时跑）
```powershell
$env:MATRAG_BOARD_HOST='192.168.5.25'   # 多主机可追加参数
bash scripts/deploy_inventory.sh 192.168.5.25
```
预期产出：单元清单、端口监听（8080/8090/8000/5432）、进程、`/opt/aterag`、`/var/log/aterag`、crontab。把输出粘进部署记录。

### Step 2 停服（先停后删）
```powershell
$env:MATRAG_BOARD_HOST='192.168.5.25'
$env:MATRAG_CLEANUP_CONFIRM='yes'
$env:MATRAG_CLEANUP_BACKUP_DIR='F:\Workspace\MATRag\data\backups\aterag-2026-10-11'
bash scripts/deploy_cleanup.sh --hosts 192.168.5.25 --backup-only   # 第一步：只备份
bash scripts/deploy_cleanup.sh --hosts 192.168.5.25 --stop-services
```
- 脚本默认 dry-run：**不带 `--stop-services/--drop-db/--remove-dirs/--remove-units` 任何动作 flag 时只打印计划**
- 动数据前必须先跑过 `--backup-only` 且备份文件非空（脚本会校验，空备份拒绝继续）

### Step 3 数据去留决策点
- **保留导出**：`pg_dump` 全库 `power_specs` + `tar` `/opt/aterag` `/var/log/aterag` → 备份目录（默认行为）
- **删除**：仅在备份成功且已确认后再 `--drop-db --remove-dirs --remove-units`
- [ ] 备份完成并校验（大小 >0，psql 可读 dump 头）

### Step 4 删除（备份成功后）
```powershell
bash scripts/deploy_cleanup.sh --hosts 192.168.5.25 --drop-db --remove-dirs --remove-units
```

### Step 5 验证释放（不过不放行）
- [ ] `ss -ltnp`：8080/8090 无监听
- [ ] `psql -l | grep power_specs` 无输出
- [ ] `systemctl list-units | grep -i aterag` 无输出；无 aterag 进程
- [ ] `ls -d /opt/aterag /var/log/aterag` 不存在
- [x] ate-cloud 已按用户裁定一并移除（2026-10-16）；保留复用项仅 PG17、uv、`/opt/python`
- [ ] 以上 5 项截图/输出贴入部署记录

### Step 6 放行
全部打勾后，才允许开始 MATRag prod 部署（见 `deploy/native/README` 与 TODO P4-3）。

## 回滚
任一步失败：按 `deploy/ROLLBACK.md` 用 Step 3 备份恢复（`pg_restore` 建库 → 还原 unit 文件 → `systemctl enable --now aterag-mcp`）。

## 纪律
- 本闸门脚本**只允许在板卡部署前执行**，不允许当作日常运维工具。
- 所有清理动作必须可追溯到本清单的某一步；清单之外的路径/单元不许删（发现未登记资产 → 先盘点再决策）。

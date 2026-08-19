# MinerU 3.4.4 源码挂载模式部署操作卡（方式 B）

> **面向对象**：内网运维/开发人员
> **预计耗时**：10-15 分钟
> **前提**：服务器已通过**方式 A** 部署过——已有 `mineru:3.4.4-upstream` 镜像 + 模型已解压到 `/data/mineru_models`
> **适用场景**：需要**频繁更新代码**（跟踪 develop 分支），改代码不重打镜像
> **与方式 A 的区别**：方式 A 把代码打进镜像，改代码要重新构建+导出镜像；方式 B 把源码挂载进容器，改代码只需换源码包 + restart。

---

## 一、物料清单

方式 B **只发源码 + 配置**，不重复发镜像 / 模型（这两样服务器上已有）：

```
offline-package-mount/
├── mineru-src-3.4.4.tar.gz       ← 源码包（解压后挂载）
├── compose-mount.yaml             ← 启动配置（含源码挂载）
├── mineru-prod-template.json      ← 配置文件模板
├── check_server_env.sh            ← 环境检查脚本
├── README-mount-deploy.md         ← 本文件
└── MANIFEST-mount.txt             ← 物料清单
```

---

## 二、首次切换（服务器已用方式 A 部署过）

### 1. 环境检查

```bash
cd /data/offline-package-mount
bash check_server_env.sh
```

**重点看**：`mineru:3.4.4-upstream` 镜像是否存在。没有 → 先按方式 A 部署（见 `../offline-package/README-internal-deploy.md`）。

### 2. 解压源码包（方式 B 独有）

```bash
mkdir -p /data/mineru-src
tar xzf /data/mineru-src-3.4.4.tar.gz -C /data/mineru-src
# 得到 /data/mineru-src/mineru/，正好是 compose 挂载源
```

> 挂载后容器会**直接使用这份源码**，覆盖镜像里自带的旧代码。

### 3. 修改配置

```bash
cp mineru-prod-template.json /root/mineru.json
# 确认 models-dir 两处路径为 /data/mineru_models/...（默认无需改）
```

### 4. 修改 compose-mount.yaml（3 处）

1. **GPU 编号**：`device_ids: ["0"]` → 你的 GPU
2. **端口**：`ports: - "8000:8000"` 有冲突时改第一个数字
3. **源码路径**：`- /data/mineru-src/mineru:...` 左半边改成实际源码路径（与第 2 步一致）

### 5. 启动

```bash
mkdir -p /data/mineru_output
docker compose -f compose-mount.yaml up -d
docker compose -f compose-mount.yaml logs -f   # 等出现 "Uvicorn running on..."
```

### 6. 验证

```bash
curl http://localhost:8000/health        # 返回 {"status":"ok"}
curl -X POST http://localhost:8000/file_parse -F "file=@/path/to/any.pdf" -o /tmp/test.zip
```

---

## 三、日常更新代码（方式 B 的核心优势）

改代码后**无需重打镜像**，三步即可生效：

```bash
# 1. 收到新源码包后重新解压覆盖
tar xzf /data/mineru-src-3.4.4.tar.gz -C /data/mineru-src

# 2. 重启容器（源码挂载自动生效）
docker compose -f compose-mount.yaml restart

# 3. 确认健康
curl http://localhost:8000/health
```

> 只改 `mineru/` 源码 → 换源码包 + restart；改了 `pyproject.toml` 依赖 → 仍需重新构建镜像（回到方式 A 流程）。

---

## 四、常用运维命令

| 操作 | 命令 |
|------|------|
| 查看状态 | `docker compose -f compose-mount.yaml ps` |
| 查看日志 | `docker compose -f compose-mount.yaml logs --tail 100` |
| 重启 | `docker compose -f compose-mount.yaml restart` |
| 停止 | `docker compose -f compose-mount.yaml down` |

---

## 五、遇到问题？

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 改的代码没生效 | 源码路径没挂对 / 没 restart | 确认 compose 源码路径与实际一致，`docker compose ... restart` |
| 导入新模块报 ModuleNotFoundError | 新文件没同步 | 重新解压源码包覆盖（`tar xzf` 会覆盖） |
| 健康检查失败 | 模型还在加载 | 再等 1-2 分钟 |
| `nvidia` 驱动错误 | Docker 无法访问 GPU | 安装 `nvidia-container-toolkit` |

---

> **方式 A（生产离线包）**：见 `../offline-package/README-internal-deploy.md`
> **完整文档**：`deploy/mineru-3.4.4-offline-deployment-guide.md`

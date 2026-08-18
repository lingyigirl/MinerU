# MinerU 3.4.4 源码挂载模式部署操作卡（方式 B）

> **面向对象**：内网运维/开发人员
> **适用场景**：需要**频繁更新代码**（跟踪 develop 分支），每次改代码不想重打 14GB 镜像
> **与方式 A 的区别**：方式 A 把代码打进镜像，改代码要重新构建+导出镜像；方式 B 把源码挂载进容器，改代码只需 `rsync + restart`。

---

## 一、物料清单

方式 B 与方式 A **共用**基础镜像和模型，只需额外准备一份源码目录：

```
offline-package-mount/
├── mineru-3.4.4-upstream.tar.gz  ← 基础镜像（软链接 → ../offline-package/）
├── mineru-models-3.4.4.tar.gz    ← 模型（软链接 → ../offline-package/）
├── compose-mount.yaml             ← 启动配置（含源码挂载）
├── mineru-prod-template.json      ← 配置文件模板
├── check_server_env.sh            ← 环境检查脚本
├── README-mount-deploy.md         ← 本文件
└── mineru/                        ← 需要额外 rsync 的源码目录（不随包分发）
```

> **传输注意**：两个 tar.gz 是软链接，指向 `../offline-package/`。传输时必须把 `offline-package/` 和 `offline-package-mount/` **两个目录一起传到同一父目录**（如 `/data/`），软链接才有效：
> `scp -r offline-package offline-package-mount root@内网服务器IP:/data/`

---

## 二、首次安装（一次性，约 15-20 分钟）

### 1. 环境检查

```bash
cd /data/offline-package-mount
bash check_server_env.sh
```

### 2. 导入基础镜像

```bash
gunzip -c mineru-3.4.4-upstream.tar.gz | docker load
docker images mineru:3.4.4-upstream   # 应看到镜像
```

### 3. 解压模型

```bash
mkdir -p /data/mineru_models
tar xzf mineru-models-3.4.4.tar.gz -C /data/mineru_models
```

### 4. 放置源码（关键，方式 B 独有）

把最新的 `mineru/` 源码放到宿主机，作为挂载源：

```bash
mkdir -p /data/mineru-src
# 方式一：从开发机 rsync（推荐）
rsync -avz --delete 开发机:/zhangbo/MinerU/mineru/ /data/mineru-src/mineru/
# 方式二：从 git 仓库 clone 后拷贝
# git clone <仓库> && cp -r MinerU/mineru /data/mineru-src/
```

> 挂载后容器会**直接使用这份源码**，镜像里自带的旧代码被覆盖，因此镜像无需 bake 最新代码。

### 5. 修改配置

```bash
cp mineru-prod-template.json /root/mineru.json
# 确认 models-dir 两处路径为 /data/mineru_models/...（默认无需改）
```

### 6. 修改 compose-mount.yaml

至少要改 **3 处**：

1. **GPU 编号**：`device_ids: ["0"]` → 你的 GPU
2. **端口**：`ports: - "8000:8000"` 有冲突时改第一个数字
3. **源码路径**：`- /data/mineru-src/mineru:...` 左半边改成实际源码路径（与第 4 步一致）

### 7. 启动

```bash
mkdir -p /data/mineru_output
docker compose -f compose-mount.yaml up -d
docker compose -f compose-mount.yaml logs -f   # 等出现 "Uvicorn running on..."
```

### 8. 验证

```bash
curl http://localhost:8000/health        # 返回 {"status":"ok"}
curl -X POST http://localhost:8000/file_parse -F "file=@/path/to/any.pdf" -o /tmp/test.zip
```

---

## 三、日常更新代码（方式 B 的核心优势）

改代码后**无需重打镜像**，三步即可生效：

```bash
# 1. 同步最新源码（开发机 → 内网服务器）
rsync -avz --delete 开发机:/zhangbo/MinerU/mineru/ /data/mineru-src/mineru/

# 2. 重启容器（源码挂载自动生效）
docker compose -f compose-mount.yaml restart

# 3. 确认健康
curl http://localhost:8000/health
```

> 只改 `mineru/` 源码 → 只需 restart；改了 `pyproject.toml` 依赖 → 仍需重新构建镜像（回到方式 A 流程）。

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
| 导入新模块报 ModuleNotFoundError | 新文件没同步 | `rsync --delete` 确保新增文件也同步过去 |
| 健康检查失败 | 模型还在加载 | 再等 1-2 分钟 |
| `nvidia` 驱动错误 | Docker 无法访问 GPU | 安装 `nvidia-container-toolkit` |

---

> **方式 A（生产离线包）**：见 `README-internal-deploy.md`。
> **完整文档**：`deploy/mineru-3.4.4-offline-deployment-guide.md`

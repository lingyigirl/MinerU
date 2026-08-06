# MinerU 3.4.4 内网离线部署操作卡

> **面向对象**：内网运维人员（无需了解 MinerU 细节）
> **预计耗时**：15-20 分钟
> **前提**：已收到 `offline-package/` 目录的完整物料包

---

## 你的物料包里有什么

```
offline-package/
├── mineru-3.4.4-upstream.tar.gz  ← Docker 镜像（约 10GB）
├── mineru-models-3.4.4.tar.gz    ← AI 模型文件（约 3GB）
├── mineru-prod-template.json     ← 配置文件模板
├── compose-prod.yaml             ← 启动配置
├── check_server_env.sh           ← 环境检查脚本
├── MANIFEST.txt                  ← 物料清单
└── README-internal-deploy.md     ← 本文件
```

---

## 第一步：环境检查

```bash
cd /data/offline-package
bash check_server_env.sh
```

**关键指标**：
- 需要 NVIDIA GPU（显存 ≥ 10GB）
- 内存 ≥ 16GB
- 磁盘可用 ≥ 50GB

如果检查未通过，先解决报错项再继续。

---

## 第二步：导入 Docker 镜像

```bash
gunzip -c mineru-3.4.4-upstream.tar.gz | docker load
```

验证：
```bash
docker images mineru:3.4.4-upstream
# 应该看到一行镜像信息
```

---

## 第三步：解压模型文件

```bash
mkdir -p /data/mineru_models
tar xzf mineru-models-3.4.4.tar.gz -C /data/mineru_models
```

验证：
```bash
ls /data/mineru_models/hub/models/OpenDataLab/
# 应该看到 PDF-Extract-Kit-1___0 和 MinerU2___5-Pro-2605-1___2B
```

---

## 第四步：修改配置文件

打开 `mineru-prod-template.json`，确认两个模型路径与实际解压位置一致（默认无需修改）：

```json
"models-dir": {
    "pipeline": "/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",
    "vlm": "/data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"
}
```

将文件放到 `/root/mineru.json`：
```bash
cp mineru-prod-template.json /root/mineru.json
```

---

## 第五步：修改 compose-prod.yaml

至少要改 **2 处**：

1. **GPU 编号**：找到 `device_ids: ["0"]`，改成你要用的 GPU 编号
2. **端口**：如果有冲突，改 `ports: - "8000:8000"` 的第一个数字

---

## 第六步：启动服务

```bash
# 创建输出目录
mkdir -p /data/mineru_output

# 启动
docker compose -f compose-prod.yaml up -d

# 查看日志（等 2-3 分钟直到出现 "Uvicorn running on..."）
docker compose -f compose-prod.yaml logs -f
```

按 `Ctrl+C` 退出日志查看，服务在后台继续运行。

---

## 第七步：验证

```bash
# 健康检查
curl http://localhost:8000/health
# 返回 {"status":"ok"} 表示正常

# 测试解析一个 PDF
curl -X POST http://localhost:8000/file_parse \
  -F "file=@/path/to/any.pdf" \
  -o /tmp/test.zip
# 返回一个 zip 文件表示正常
```

---

## 常用运维命令

| 操作 | 命令 |
|------|------|
| 查看状态 | `docker compose -f compose-prod.yaml ps` |
| 查看日志 | `docker compose -f compose-prod.yaml logs --tail 100` |
| 重启服务 | `docker compose -f compose-prod.yaml restart` |
| 停止服务 | `docker compose -f compose-prod.yaml down` |

---

## 遇到问题？

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 健康检查失败 | 模型还在加载 | 再等 1-2 分钟 |
| 端口被占用 | 其他服务用了 8000 | 改 compose 中的端口映射 |
| `nvidia` 驱动错误 | Docker 无法访问 GPU | 安装 `nvidia-container-toolkit` |
| 显存不足 | GPU 被其他进程占满 | 用 `nvidia-smi` 检查，换空闲 GPU |
| 解析结果为空 | PDF 损坏或有密码 | 换一个正常 PDF 测试 |

---

> **完整文档**：如需了解多 GPU 部署、C/S 分离、性能调优等高级内容，
> 请参考 `deploy/mineru-3.4.4-offline-deployment-guide.md`

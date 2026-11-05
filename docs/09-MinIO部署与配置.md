# MinIO 部署与配置文档（针对本机环境）

> 本文档按当前机器的实际环境编写，可直接照做。
> 编写日期：2026-10-25
>
> **⚠️ 本编排（`deploy/minio/`）是项目唯一的 MinIO 定义**（2026-10-27 收敛）：主 `deploy/docker-compose.yaml` 不再包含 MinIO 服务——历史上两套定义并存过，容器同名 `bid-minio` 会冲突，且主 compose 曾使用已废弃的 `minio/minio:latest` 标签（拉取报 not found）。凭证以本目录 `.env` 为权威，引擎侧（`review-engine/.env`、`src/common/config.py` 默认值）必须与它一致。

---

## 一、本机环境

| 项目 | 实际情况 |
|------|----------|
| 操作系统 | Windows（Docker Desktop，WSL2 后端） |
| 架构 | x86_64 / AMD64 |
| Docker 版本 | 29.7.2 |
| Docker Compose | v5.3.1 |
| CPU / 内存 | 8 核 / 16 GB |
| Docker Desktop 安装路径 | `C:\Users\gothgemini\AppData\Local\Programs\DockerDesktop\` |
| 项目根目录 | `E:\project\bid-review` |
| MinIO 数据目录 | `E:\project\bid-review\data\minio` |
| 端口占用情况 | 5432（PostgreSQL）、6379（Redis）已占用，**9000/9001 空闲** |

---

## 二、方案设计

采用 **Docker Compose** 编排单节点 MinIO，理由：

- 配置集中在 `docker-compose.yml`，可版本化、可复用
- 数据持久化到宿主机目录，容器删除后数据不丢
- `restart: unless-stopped`，开机/崩溃后自动拉起
- 后续要扩成多节点集群，只需改 compose 文件，不用重构

### 端口规划

| 端口 | 用途 | 访问地址 |
|------|------|----------|
| 9000 | S3 API（程序接入） | `http://localhost:9000` |
| 9001 | Web 管理控制台（浏览器） | `http://localhost:9001` |

### 目录结构

```
E:\project\bid-review\
├── deploy\minio\
│   ├── docker-compose.yml   # 编排配置
│   └── .env                 # 账号密码（勿提交仓库）
└── data\minio\              # 对象数据持久化目录（容器内 /data）
```

---

## 三、部署步骤

### 3.1 前置：启动 Docker Desktop

双击桌面/开始菜单的 **Docker Desktop** 图标，等右下角鲸鱼图标变绿（状态栏显示 Engine running）。

验证：

```bash
docker info
```

能打印出 Server 段信息即为就绪。

> ⚠️ 本机 Docker Desktop 的 WSL2 后端依赖 `wsl.exe`。若 Docker Desktop 启动后自动退出，检查是否有安全软件拦截了 `wsl.exe`。

### 3.2 检查镜像加速器（重要，本机踩过坑）

本机原先配置的三个加速源已全部失效：

| 原加速源 | 实测结果 |
|----------|----------|
| `https://docker.xuanyuan.me` | HTTP 401（已改为需专业版认证） |
| `https://docker.1ms.run` | HTTP 401（需认证） |
| `https://docker.m.daocloud.io` | 无响应 |

**已替换为**（`C:\Users\gothgemini\.docker\daemon.json`）：

```json
{
  "builder": {
    "gc": { "defaultKeepStorage": "20GB", "enabled": true }
  },
  "experimental": false,
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://atomhub.openatom.cn"
  ]
}
```

> 修改 daemon.json 后必须重启 Docker Desktop 才生效。
> 公共加速源不确定性高，配置前建议先探测：
> `curl -s -o /dev/null -m 8 -w "%{http_code}" https://docker.1panel.live/v2/`，返回 200 才可用。

### 3.3 启动 MinIO

```bash
cd E:/project/bid-review/deploy/minio
docker compose up -d
```

首次执行会拉取镜像（约 100MB），耐心等待。

### 3.4 验证

```bash
# 容器状态，应显示 Up (healthy)
docker compose ps

# 查看启动日志
docker compose logs -f minio
```

浏览器打开 `http://localhost:9001`，用 `.env` 里的账号登录：

| 项 | 值 |
|----|-----|
| 用户名 | `minioadmin` |
| 密码 | `minioadmin123` |

---

## 四、配置文件说明

### docker-compose.yml

```yaml
services:
  minio:
    image: minio/minio:RELEASE.2025-04-22T22-12-26Z
    container_name: bid-minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
      MINIO_REGION: ${MINIO_REGION:-cn-north-1}
    ports:
      - "9000:9000"
      - "9001:9001"
    volumes:
      - E:/project/bid-review/data/minio:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
```

**关键点说明：**

- **镜像标签不用 `latest`**：MinIO 官方已停止维护 `latest` 标签，必须用带日期的 RELEASE 标签，否则报 `not found`。
- **`--console-address ":9001"`**：显式指定控制台端口，避免新版 MinIO 随机分配端口导致找不到 Web UI。
- **`MINIO_REGION`**：影响 S3 SDK 签名校验，设成固定值可避免部分 SDK 报签名区域不匹配。
- **`restart: unless-stopped`**：手动 `docker stop` 后不会自动拉起，但机器重启或异常退出会自动恢复，符合生产习惯。

### .env

```ini
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin123
MINIO_REGION=cn-north-1
```

> MinIO 强制要求 root 密码 **至少 8 位**，否则容器启动失败并报 `Invalid credentials`。

**改密码流程：**

```bash
# 1. 编辑 .env 里的 MINIO_ROOT_PASSWORD
# 2. 重启容器
docker compose up -d --force-recreate
```

---

## 五、常用运维命令

```bash
cd E:/project/bid-review/deploy/minio

docker compose up -d            # 启动
docker compose ps               # 查看状态
docker compose logs -f minio    # 实时日志
docker compose restart minio    # 重启
docker compose stop             # 停止（保留容器）
docker compose down             # 停止并删除容器（数据保留在宿主机）
docker compose down -v          # 连同数据卷一起删除（⚠️ 数据丢失，慎用）
```

---

## 六、客户端接入

### 6.1 安装 mc 命令行工具

```bash
docker run --rm -it --entrypoint=/bin/sh minio/mc:RELEASE.2025-04-22T22-12-26Z
```

或者直接在容器内用 MinIO 自带的 `mc`：

```bash
docker exec -it bid-minio mc --version
```

### 6.2 创建 Bucket（用 mc）

```bash
# 注册别名
docker exec -it bid-minio mc alias set local http://localhost:9000 minioadmin minioadmin123

# 建桶（招投标文件建议按类型分桶）
docker exec -it bid-minio mc mb local/bid-documents
docker exec -it bid-minio mc mb local/bid-attachments

# 设置桶策略为可匿名下载（如需要，谨慎）
docker exec -it bid-minio mc anonymous set download local/bid-documents

# 查看桶列表
docker exec -it bid-minio mc ls local
```

### 6.3 Python SDK 接入（适配 bid-review 项目）

安装依赖：

```bash
pip install minio
```

示例代码：

```python
from minio import Minio
from minio.error import S3Error
import io

client = Minio(
    "localhost:9000",
    access_key="minioadmin",
    secret_key="minioadmin123",
    secure=False,          # 本地开发用 HTTP；生产上 TLS 后改 True
    region="cn-north-1",
)

BUCKET = "bid-documents"

# 确保桶存在
if not client.bucket_exists(BUCKET):
    client.make_bucket(BUCKET)

# 上传招标文件
def upload_file(local_path: str, object_name: str):
    client.fput_object(
        BUCKET, object_name, local_path,
        content_type="application/pdf",
    )
    print(f"已上传: {object_name}")

# 下载
def download_file(object_name: str, save_path: str):
    client.fget_object(BUCKET, object_name, save_path)

# 生成临时分享链接（有效期 7 天）
def presigned_url(object_name: str, days: int = 7):
    from datetime import timedelta
    return client.presigned_get_object(
        BUCKET, object_name, expires=timedelta(days=days)
    )

# 列举文件
def list_files(prefix: str = ""):
    for obj in client.list_objects(BUCKET, prefix=prefix, recursive=True):
        print(f"{obj.object_name}  {obj.size} bytes")


if __name__ == "__main__":
    try:
        upload_file("E:/project/bid-review/data/raw/example.pdf", "2026/example.pdf")
        list_files()
    except S3Error as e:
        print(f"MinIO 错误: {e}")
```

### 6.4 环境变量配置建议

在项目的 `.env` 或配置文件中加入：

```ini
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin123
MINIO_BUCKET=bid-documents
MINIO_SECURE=false
```

---

## 七、故障排查

### 问题 1：镜像拉取报 `not found`

```
docker.io/minio/minio:latest: not found
```

**原因**：MinIO 已弃用 `latest` 标签。
**解决**：改用带日期的版本标签，如 `RELEASE.2025-04-22T22-12-26Z`。可用标签见 Docker Hub 的 tags 页面。

### 问题 2：拉取报「免费节点当前繁忙」或 401

```
error from registry: 免费节点当前繁忙，请稍后重试
```

**原因**：配置的镜像加速源已失效或改为收费认证。
**解决**：
1. 逐个探测加速源：`curl -s -o /dev/null -m 8 -w "%{http_code}" <源地址>/v2/`
2. 只保留返回 200 的源，写入 `~/.docker/daemon.json`
3. 重启 Docker Desktop

### 问题 3：容器启动即退出，日志报 `Invalid credentials`

**原因**：root 密码不足 8 位。
**解决**：修改 `.env` 中 `MINIO_ROOT_PASSWORD` 为 8 位以上。

### 问题 4：浏览器打不开 9001 控制台

**排查顺序**：
```bash
docker compose ps                    # 容器是否 Up
docker compose logs minio | tail -30 # 是否有报错
netstat -ano | findstr :9001         # 端口是否被占用
```
确认 `--console-address ":9001"` 参数存在，且 compose 里做了 `9001:9001` 端口映射。

### 问题 5：Windows 宿主目录挂载后数据看不到

Docker Desktop 默认会把 `E:\` 盘加入共享。若挂载失败：
Docker Desktop → Settings → Resources → File sharing，确认 E 盘在共享列表中。

### 问题 6：Docker Desktop 启动后自动退出

**原因**：WSL2 后端异常，常见于 `wsl.exe` 被安全软件/组策略拦截。
**解决**：
1. 检查杀毒软件是否拦截 `wsl.exe`
2. 命令行执行 `wsl --update` 更新 WSL
3. Docker Desktop → Settings → General → 勾选 "Use the WSL 2 based engine"
4. 必要时 Docker Desktop → Troubleshoot → Restart / Reset to factory defaults

---

## 八、生产环境加固建议

当前配置是**开发/内网环境**的合理默认值。若要对公网暴露或承载正式业务，需补做：

1. **启用 HTTPS**
   MinIO 支持挂载证书到 `/root/.minio/certs/`，放入 `public.crt` 和 `private.key` 即可自动启用 TLS。前置 Nginx 反向代理也可。

2. **修改默认账号密码**
   `minioadmin/minioadmin123` 仅用于本地开发，任何对外环境必须换成强密码（建议 16 位以上随机串）。

3. **不要把 9000/9001 直接暴露公网**
   内网部署时用防火墙限制来源 IP；公网访问走 VPN 或反向代理 + 认证。

4. **改单节点为分布式集群**
   单节点无冗余，磁盘故障即数据丢失。生产建议至少 4 节点，每节点 4 块盘，用纠删码保证可用性。

5. **配置数据备份**
   用 `mc mirror` 定期同步到异地：
   ```bash
   mc mirror --overwrite local/bid-documents backup/bid-documents
   ```

6. **开启版本控制**
   防止误删误覆盖：
   ```bash
   mc version enable local/bid-documents
   ```

7. **限制 Bucket 匿名访问**
   除非确需公开下载，否则保持桶为 private，用预签名 URL 做临时授权。

---

## 九、快速检查清单

部署完成后逐项核对：

- [ ] `docker compose ps` 显示 `Up (healthy)`
- [ ] 浏览器能打开 `http://localhost:9001` 并登录成功
- [ ] 控制台里能创建 Bucket
- [ ] Python SDK 能上传/下载文件
- [ ] `E:\project\bid-review\data\minio` 下能看到生成的数据目录
- [ ] 重启 Docker Desktop 后容器自动恢复（验证 `restart` 策略）

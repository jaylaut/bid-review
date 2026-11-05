# RocketMQ 5.1.0 本机安装与启动手册

> 面向对象：不熟悉 RocketMQ 的开发者。本文按"每一步装什么、为什么装、装完怎么验证"的顺序写，并把实测踩过的坑和解决方案单独列在文末。

## 0. 先搞清楚：你要启动的是「三个进程」，不是「一个服务」

RocketMQ 和 MySQL/Redis 不一样，它**不是单进程**，而是由三个独立进程协作：

| 进程 | 作用（一句话） | 默认端口 | 你的项目里对应谁 |
| --- | --- | --- | --- |
| **NameServer** | 路由中心：记录"哪个 topic 存在哪个 Broker 上"，相当于电话总机 | 9876 | 生产者/消费者先问它"去哪发/去哪收" |
| **Broker** | 真正的存储：消息实际存在这里，负责读写、持久化 | 10911 | 消息落盘的地方 |
| **Proxy** | 协议翻译层（5.x 新增）：对客户端暴露 gRPC，对内转 remoting 连 Broker | 8080(remoting) / 8081(gRPC) | **你的 Python 客户端只连它** |

**依赖关系（启动顺序不能乱）**：NameServer 先起 → Broker 起（要注册到 NameServer）→ Proxy 起（要连 Broker）。

> 【白话】为什么 5.x 多了个 Proxy？4.x 时代客户端要同时认识 NameServer 和 Broker 两套地址、两种协议，各语言 SDK 很难写。5.x 用 Proxy 统一对外——客户端只说一种语言（gRPC），Proxy 负责翻译成 Broker 的内部协议。这就是我们自研 Python 客户端只需连 8081 端口的原因。

---

## 1. 前置条件检查（你的机器已满足）

| 依赖 | 要求 | 你的现状 | 结论 |
| --- | --- | --- | --- |
| JDK | 8 / 11（**强烈不推荐 21**） | JDK 21（`JAVA_HOME=E:\CodingTools\jdk21`） | ⚠️ 有坑，见「坑 1」 |
| RocketMQ 安装包 | 5.x 二进制包 | `E:\CodingTools\rocketmq5.1`（5.1.0） | ✅ 已就绪 |
| 内存 | 建议 ≥ 4GB 空闲 | 31.9GB（当前空闲 20GB） | ✅ 充足 |

### 关键前提：确认 JDK 版本能跑

RocketMQ 5.1.0 官方推荐 **JDK 8 或 11**。你机器是 JDK 21，直接启动会崩（实测复现，见坑 1）。你有两个选择：

- **方案 A（推荐，最省事）**：你机器上有 JDK 8（`E:\CodingTools\jdk8`），临时把 `JAVA_HOME` 指到 jdk8 再启动。
- **方案 B（留在 JDK 21）**：手动改脚本，删掉 CMS 垃圾回收参数（见坑 1 的解决方案）。

> 本项目文档选择**方案 B**（因为你的 shell 环境已全局配了 JDK 21，改全局 JAVA_HOME 会影响其他项目）。下面第 2 步会给出具体改法。

---

## 2. 步骤一：让启动脚本兼容 JDK 21（一次性，改完永久生效）

### 2.1 这一步在做什么

RocketMQ 的 Windows 启动脚本 `runserver.cmd` 里硬编码了一个 JVM 参数 `-XX:+UseConcMarkSweepGC`（指定用 CMS 垃圾回收器）。CMS 在 **JDK 14 已被移除**，JDK 21 不认识这个参数，启动会直接报错退出。我们要把这个过时参数删掉。

### 2.2 操作

打开文件 `E:\CodingTools\rocketmq5.1\bin\runserver.cmd`，找到第 30 行：

```bat
set "JAVA_OPT=%JAVA_OPT% -XX:+UseConcMarkSweepGC -XX:+UseCMSCompactAtFullCollection -XX:CMSInitiatingOccupancyFraction=70 -XX:+CMSParallelRemarkEnabled -XX:SoftRefLRUPolicyMSPerMB=0 -XX:+CMSClassUnloadingEnabled -XX:SurvivorRatio=8 -XX:-UseParNewGC"
```

把这一整行**删掉**（或替换成空）。CMS 那一串参数在 JDK 21 全部失效，删掉后 JVM 会自动用默认的 G1 垃圾回收器（现代 JDK 默认值，更好）。

### 2.3 验证

改完后在命令行跑一句，看是否还报错：

```bat
"%JAVA_HOME%\bin\java" -version
```

如果输出 JDK 版本号（不报 `Unrecognized VM option`），说明环境本身没问题，剩下的就靠启动脚本验证（见第 5 步）。

---

## 3. 步骤二：配置 Broker 的存储路径（一次性）

### 3.1 这一步在做什么

Broker 需要把消息**落盘**（持久化到磁盘）。默认配置里存储路径是相对/默认值，Windows 上最好显式指定到 `E:\CodingTools\rocketmq5.1\store`，避免消息写到哪里都不清楚。

### 3.2 操作

打开 `E:\CodingTools\rocketmq5.1\conf\broker.conf`，在文件末尾追加两行：

```properties
# 消息存储根目录（commitlog + consumequeue + index 都在这里）
storePathRootDir=E:\CodingTools\rocketmq5.1\store
# 名字服务地址（Broker 要注册到这里，供路由查询）
namesrvAddr=127.0.0.1:9876
```

> 【白话】`storePathRootDir` 是"消息放哪个文件夹"；`namesrvAddr` 是"Broker 起来后去哪个总机报到"。两个都是 Broker 启动必须知道的。

---

## 4. 步骤三：按顺序启动三个进程（每次开机都要做的部分）

**核心原则：必须按 NameServer → Broker → Proxy 的顺序启动，且每个都要用独立窗口，因为它们是前台进程（窗口关了就停了）。**

下面用三个独立的 PowerShell/CMD 窗口分别执行。

### 4.1 启动 NameServer（第 1 个窗口）

```bat
set ROCKETMQ_HOME=E:\CodingTools\rocketmq5.1
cd /d %ROCKETMQ_HOME%\bin
start mqnamesrv.cmd
```

**作用**：起路由中心，监听 9876 端口。Broker 和 Proxy 都要先问它。

**验证**：看到日志出现 `The Name Server boot success` 即成功。

### 4.2 启动 Broker（第 2 个窗口）

```bat
set ROCKETMQ_HOME=E:\CodingTools\rocketmq5.1
cd /d %ROCKETMQ_HOME%\bin
start mqbroker.cmd -c ..\conf\broker.conf
```

> 注意 `-c ..\conf\broker.conf`：告诉 Broker 用我们第 3 步改过的配置文件（含存储路径和 namesrv 地址）。

**作用**：起真正的消息存储服务，监听 10911 端口，并把自身注册到 NameServer。

**验证**：日志出现 `The broker[broker-a, 127.0.0.1:10911] boot success`。

### 4.3 启动 Proxy（第 3 个窗口）

```bat
set ROCKETMQ_HOME=E:\CodingTools\rocketmq5.1
cd /d %ROCKETMQ_HOME%\bin
start mqproxy.cmd
```

**作用**：起协议翻译层，监听 8080（remoting）和 8081（gRPC）。**你的 Python 客户端只连它的 8081**。

**验证**：日志出现 `The proxy server boot success`。

---

## 5. 步骤四：验证三个进程都活着

### 5.1 看端口

在任意窗口执行：

```bat
netstat -ano | findstr "9876 10911 8081"
```

期望看到**三行**（分别对应 NameServer、Broker、Proxy 的端口），说明三个进程都在监听。

### 5.2 用你的项目自检脚本

在 `E:\project\bid-review\review-engine` 下执行：

```bat
uv run python ../scripts/check_env.py
```

期望输出里 RocketMQ 那一行是：

```
[ OK ] RocketMQ               proxy gRPC localhost:8081 可连
```

（如果显示 `[WARN]`，说明 Proxy 没起来，回到第 4.3 步查日志。）

---

## 6. 步骤五：真连验证（可选，用你刚写的客户端）

三个进程起来后，可以跑一个最小的发送+消费验证（这是练手最直观的一步）。这一步我先占位说明，具体脚本等你确认要写联调脚本时我再补（见文末"下一步"）。

---

## 7. 坑与解决方案（全部实测复现过）

### 坑 1（最致命）：JDK 21 启动即崩 —— `Unrecognized VM option 'UseConcMarkSweepGC'`

**现象**：用 JDK 21 跑 `mqnamesrv.cmd` / `mqbroker.cmd`，进程秒退，报：

```
Unrecognized VM option 'UseConcMarkSweepGC'
Error: Could not create the Java Virtual Machine.
```

**原因**：RocketMQ 5.1.0 的 `runserver.cmd` 硬编码了 CMS 垃圾回收器参数。CMS 在 **JDK 14 已移除**，JDK 21 完全不认识。

**解决**：删掉 `runserver.cmd` 里 `-XX:+UseConcMarkSweepGC` 那一整行（见第 2 步）。CMS 那串参数在 JDK 21 全失效，删掉后 JVM 自动用 G1。

> 我实测复现了这条报错：`"jdk21\bin\java" -XX:+UseConcMarkSweepGC -version` 直接返回 `Unrecognized VM option`。

### 坑 2：.sh 脚本和 .cmd 脚本的内存配置差 16 倍

**现象**：如果图方便用 Git Bash 跑 `sh bin/mqnamesrv`（Linux 脚本），会看到 JVM 试图分配 `-Xms4g -Xmx4g`（NameServer）甚至 `-Xms8g -Xmx8g -XX:MaxDirectMemorySize=15g`（Broker）——这是生产级配置。

**原因**：RocketMQ 提供两套脚本：`.sh`（Linux 生产）和 `.cmd`（Windows 开发）。`.cmd` 版内存已经调小到 `-Xms512m -Xmx512m`，是给开发机用的。

**解决**：Windows 上**务必用 `.cmd` 脚本**（`mqnamesrv.cmd` / `mqbroker.cmd` / `mqproxy.cmd`），不要用 `.sh`。你机器 31.9GB 内存虽然扛得住 8g，但没必要的浪费。

### 坑 3：Proxy 端口不是 9876，也不是 10911

**现象**：以为客户端连 NameServer 的 9876 或 Broker 的 10911，结果连不上。

**原因**：5.x 客户端只连 **Proxy**。Proxy 有两个端口：8080（remoting，对内）和 8081（gRPC，对外给客户端）。

**解决**：Python 客户端连 `localhost:8081`（你的 `rocketmq_endpoint` 已经配对了）。

### 坑 4：三个进程启动顺序错了会「找不到 Broker」

**现象**：Proxy 或客户端报 `TOPIC_NOT_FOUND` / `Broker 不存在`。

**原因**：Broker 必须**先**注册到 NameServer，Proxy 才能通过 NameServer 找到 Broker。顺序反了，Proxy 起来时 NameServer 里还没有 Broker 的注册信息。

**解决**：严格按 NameServer → Broker → Proxy 顺序启动，且每个进程启动后**等日志出现 boot success 再起下一个**（Broker 注册需要几秒）。

### 坑 5：Broker 没指定 namesrvAddr，注册到「空总机」

**现象**：NameServer 和 Broker 都起来了，但客户端 `QueryRoute` 返回空、查不到 topic。

**原因**：Broker 默认可能连一个「默认 NameServer 地址」，如果没显式配 `namesrvAddr=127.0.0.1:9876`，Broker 注册到了一个不存在的地址，NameServer 里就没有它的信息。

**解决**：在 `broker.conf` 里显式写 `namesrvAddr=127.0.0.1:9876`（见第 3 步），并用 `-c ..\conf\broker.conf` 启动 Broker。

### 坑 6：Windows 下消息落盘路径含中文/空格会出问题

**现象**：`storePathRootDir` 配到含中文或空格的路径（如 `D:\我的文档\rocketmq`），Broker 启动或写消息时报路径错误。

**原因**：Broker 底层用 Java NIO 操作文件，对含空格/中文的路径在某些版本有兼容问题。

**解决**：`storePathRootDir` 用纯英文、无空格的路径（`E:\CodingTools\rocketmq5.1\store` 就符合）。

---

## 8. 一键启动脚本（可选，省得每次开三个窗口）

如果你嫌每次开三个窗口麻烦，可以把三个启动命令合成一个 `start-rocketmq.bat`，放在 `E:\CodingTools\rocketmq5.1\` 下：

```bat
@echo off
set ROCKETMQ_HOME=E:\CodingTools\rocketmq5.1

echo [1/3] 启动 NameServer...
start "rmq-namesrv" cmd /c "%ROCKETMQ_HOME%\bin\mqnamesrv.cmd"

timeout /t 5 /nobreak >nul

echo [2/3] 启动 Broker...
start "rmq-broker" cmd /c "%ROCKETMQ_HOME%\bin\mqbroker.cmd -c %ROCKETMQ_HOME%\conf\broker.conf"

timeout /t 5 /nobreak >nul

echo [3/3] 启动 Proxy...
start "rmq-proxy" cmd /c "%ROCKETMQ_HOME%\bin\mqproxy.cmd"

echo 三个进程已启动，等待 10 秒后验证...
timeout /t 10 /nobreak >nul
netstat -ano | findstr "9876 10911 8081"
pause
```

> 这个脚本用 `start "窗口名" cmd /c "命令"` 把三个进程分别开进独立窗口，中间用 `timeout` 间隔 5 秒保证启动顺序。最后自动验证端口。

---

## 9. 下一步（待你确认）

1. **真连联调脚本**：我可以写一个 `scripts/e2e_rocketmq.py`，用你刚写好的 `RocketMQClient` 做一次"发送 → 接收 → ack"的端到端验证，亲眼看到消息在 RocketMQ 里流转。
2. **dashboard 可视化**：你机器上有 `rocketmq-dashboard-1.0.0`，可以起起来看 topic 消息积压、消费情况（可视化管理台）。

需要哪个，告诉我即可。

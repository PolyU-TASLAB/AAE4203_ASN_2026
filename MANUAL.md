# 使用手册 · GNSS Benchmark Challenge

启动 / 停止 / 日常维护。完整说明见 [README.md](README.md)，当天操作清单见
[docs/TA_runbook.md](docs/TA_runbook.md)。

---

## 一页速查

| 操作 | 命令 |
|---|---|
| **启动** | 双击 `start.bat`，或在本目录执行 `python run.py` |
| **停止** | 在运行窗口按 `Ctrl+C` |
| **强制停止**（找不到窗口时） | `Get-NetTCPConnection -LocalPort 8080 -State Listen \| %{ Stop-Process -Id $_.OwningProcess -Force }` |
| **检查配置** | `python run.py --check` |
| **打开榜单** | http://127.0.0.1:8080/ |
| **投影模式** | http://127.0.0.1:8080/tv |
| **教师台** | http://127.0.0.1:8080/admin |

所有命令都在项目文件夹（`E:\competition`）里执行。

---

## 一、启动

### 方式 A：双击（推荐给不熟悉命令行的人）

双击项目文件夹里的 **`start.bat`**。

它会自动找 Python、检查版本是否 ≥ 3.11，然后启动服务器。
出错时窗口会停住并显示原因，不会一闪而过。

### 方式 B：命令行

```powershell
cd E:\competition
python run.py
```

### 方式 C：后台常驻（长期运行，关掉终端也不停）

```powershell
cd E:\competition
Start-Process -FilePath 'python' -ArgumentList 'run.py' -WindowStyle Hidden
```

在校园服务器上建议用 Windows 任务计划程序设为"开机启动"，或 Linux 上用 `nohup`：

```bash
nohup python run.py > data/server.log 2>&1 &
```

### 启动成功的样子

```
====================================================================
  AAE4203 - GNSS Benchmark Challenge
====================================================================
  Leaderboard    http://172.20.234.7:8080/
  Projector      http://172.20.234.7:8080/tv
  Student rules  http://172.20.234.7:8080/rules
  Admin console  http://172.20.234.7:8080/admin
  Local          http://127.0.0.1:8080/
--------------------------------------------------------------------
  Challenge      UrbanNav Whampoa - SPP Benchmark
  Benchmark      1534 epochs, GPS week 2158
  ENU origin     22.301558, 114.190308
  Ranked when    availability >= 90 %
  Source         local mirror  E:\competition\drive_mirror
  Polling        every 10 s
  Database       E:\competition\data\competition.db
====================================================================
  Ctrl+C to stop
```

把 `http://172.20.234.7:8080/` 那行（局域网地址）抄到黑板上，学生用手机或笔记本就能看。

### 启动参数

| 参数 | 作用 |
|---|---|
| `--port 8090` | 换端口（默认 8080，被占用时会自动往上试） |
| `--host 127.0.0.1` | 只允许本机访问（默认 `0.0.0.0` 局域网可访问） |
| `--check` | 只检查配置、真值、Drive 连通性，不启动服务 |
| `--no-poller` | 只读数据库、不扫描新文件（演示或排查时用） |
| `--config 其他.toml` | 用别的配置文件 |

---

## 二、停止

### 方式 A：正常停止（推荐）

在跑着服务器的那个窗口里按 **`Ctrl+C`**。

会看到：

```
^C
shutting down ...
```

窗口回到命令提示符，进程已退出。

### 方式 B：关掉窗口

如果是用 `start.bat` 启动的，直接关闭那个命令行窗口。
（如果窗口已经找不到了，用方式 C。）

### 方式 C：强制停止（找不到窗口 / 卡住时）

**Windows PowerShell：**

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**或者用旧命令：**

```powershell
netstat -ano | findstr :8080
taskkill /PID <上面查到的PID> /F
```

**Linux / macOS：**

```bash
pkill -f "run.py"
# 或
kill $(lsof -t -i:8080)
```

### 确认真的停了

```powershell
Invoke-WebRequest http://127.0.0.1:8080/api/status -TimeoutSec 3
```

报"连接错误 / 无法连接"就是停了。能返回 JSON 说明还在跑。

### 停止会不会丢数据？

**不会。** 每次上传都已归档到 `data/submissions/`，分数存在 `data/competition.db`。
重启后榜单原样恢复。

---

## 三、重启

停止后再启动即可，没有任何特殊步骤：

```powershell
python run.py
```

重启后排名升降箭头会重置（它们表示"上一次刷新以来"的变化），分数和排名不受影响。

**改了 `config.toml` 必须重启才生效。**

---

## 四、日常自检

### 启动前/后跑一次

```powershell
python run.py --check
```

五行输出全 `OK` 就没问题：

```
config        OK
ground truth  OK  1534 epochs, week [2158], origin 22.301558/114.190308
database      OK  E:\competition\data\competition.db
source        OK  local_mirror
web assets    OK
```

`source` 那行失败时会直接告诉你是路径错了、还是 Drive 密钥没共享给服务账号。

### 运行中检查

浏览器打开 http://127.0.0.1:8080/api/status，或看榜单右上角：

- 绿点 + `live · HH:MM:SS` = 正常
- 灰点 + `reconnecting…` = 推送断了（刷新页面即可，30 秒会自动兜底轮询）

### 跑单元测试

```powershell
python -m unittest discover -s tests
```

应该是 `Ran 25 tests ... OK`。

---

## 五、常见问题

| 现象 | 原因与处理 |
|---|---|
| `'python' 不是内部或外部命令` | Python 没装或没加进 PATH。装 3.11+ 并勾选 "Add python.exe to PATH"，或直接用 `start.bat` |
| `Python 3.11 or newer is required` | 版本太旧，装新版 |
| `端口被占用` / 启动后打不开网页 | 换端口：`python run.py --port 8090` |
| 红字 `SOURCE NOT AVAILABLE` | Drive 目录不存在或没同步下来。网站仍可用（读旧数据），改好路径重启 |
| 上传了但榜单不动 | ① Drive 还没同步（等 10 秒）② 文件内容和上次完全一样（会被跳过，这是设计如此）③ 看 `/admin` 的 event log |
| 榜单某行是 `–` | 可用率低于 90%、文件无效、或还没上传，行内会写原因 |
| 某行出现红色 `!` | 防作弊标记，只提醒不扣分；点进组页面看原因 |
| 页面打不开但进程在 | 防火墙拦了 8080；或用了 `--host 127.0.0.1` 导致别人连不上 |

更详细的故障处理见 [docs/TA_runbook.md](docs/TA_runbook.md)。

---

## 六、数据在哪里 / 怎么备份

| 路径 | 内容 | 要不要备份 |
|---|---|---|
| `data/competition.db` | 全部提交记录与分数 | **要** |
| `data/submissions/` | 每次上传的原始 CSV（带 SHA-256 文件名） | **要** |
| `data/ground_truth/` | 真值（答案，绝不能外泄） | **要，且保密** |
| `drive_mirror/` | 被监听的文件夹 | 不用（Drive 上有） |
| `demo_drive/` | 模拟数据 | 不用 |

整个 `data/` 文件夹拷走就是完整备份。停止服务后再拷最稳妥。

**清空重来**（换真实数据前）：

```powershell
Remove-Item -Recurse -Force data\competition.db*, data\submissions, drive_mirror\*
```

---

## 七、换成真实数据前必做

1. 改 `config.toml` 里的 `admin_token`（别用默认的 `change-me-please`）
2. 改 `[sources]` 指向真实的 Drive 文件夹（`local_mirror` 或 `gdrive_api`）
3. 清掉演示数据（见上）
4. `python run.py --check` 全绿
5. 启动，确认榜单是空的、`/admin` 里 source 状态正常

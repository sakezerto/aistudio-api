# 自用构建说明（非上游）

这是 **个人自用** 的 aistudio-api 构建，基线是上游 `master`，改动只服务于自己部署，**不向上游提交 PR**。

## 分支布局

| 分支 | 用途 |
|---|---|
| `upstream/master` | 上游原始代码，只读，不直接在其上改动 |
| `local` | 你实际使用/构建的分支，所有自用改动都在这里 |

查看自己改了什么：

```bash
git log --oneline upstream/master..local        # 自己叠了哪些提交
git diff upstream/master                        # 累计差异
```

## 当前自用改动

1. **`6814786` fix(gateway): persist Google cookies across browser restarts**
   （cherry-pick 自 PR #11，同内容）
   - `expires: -1` 不再透传给 CDP，会话级 cookie（`OSID`/`__Secure-OSID`）不再被 Chromium 当场删除
   - `get_cookies` 改用浏览器级 `Storage.getCookies`，回退到多域 `Network.getCookies`，不再漏掉 `accounts.google.com` 根凭据
   - `save_browser_cookies` 按 `(name, domain, path)` 增量合并 + 登录页拒绝回写，不再用残缺结果全量覆盖 `auth.json`
   - `load_cookies_to_page` 在 profile 已有有效 `{SID, __Secure-1PSID, LSID}` 时跳过覆盖注入（保护 DBSC 绑定），新增 `force_inject`
   - `cleanup_stream_page` 定期回写滚动刷新的 `__Secure-1PSIDTS`
   - 新增 `tests/unit/test_cookie_persistence.py`（13 个用例）
2. **`docker-compose.local.yml`**：从本仓库源码构建 `aistudio-api-mine:latest`，配置与数据统一放在 `./data`
3. **`local/sync-upstream.sh`**：一条命令把上游新版合进来并跑测试

## 同步上游新版

```bash
bash local/sync-upstream.sh
```

它会 `git fetch upstream` → `git rebase upstream/master` → `pytest`。

如果作者后续把同样的修复合进了上游，rebase 会在 `6814786` 上产生冲突或空提交：

- 变空 → `git rebase --skip`
- 冲突 → 解冲突时以上游为准，只保留自己额外想要的部分，然后 `git rebase --continue`

## 本地跑（不用 Docker）

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test]"
bash scripts/setup-browser.sh            # 首次需要 CloakBrowser
.venv/Scripts/python.exe main.py server --port 8080
```

## Docker 跑

```bash
cp .env.example .env        # 首次，按需改
docker compose -f docker-compose.local.yml up -d --build
```

- 镜像名 `aistudio-api-mine:latest`，容器名 `aistudio-api-mine`，与上游 `aistudio-api` 不冲突
- 数据/账号/配置全在宿主机 `./data`
  - 账号凭据：`data/accounts/<acc_id>/auth.json`
  - 配置文件：`data/config.yaml`（首次启动由镜像内默认配置生成，之后直接改这个）
- 重建后账号不丢的前提就是 `./data` 这个挂载，别再额外挂 `config.yaml`
- Web 控制台：`http://localhost:8080`；需要密码就设置 `AISTUDIO_WEB_PASSWORD`

## 端口与健康检查

- 容器内固定监听 `8080`；`docker-compose` 里映射成 `8888:8080`（bridge 网络），
  外部访问用 `http://<宿主IP>:8888`。宿主侧端口不影响容器内的探活。
- 镜像自带 `HEALTHCHECK`，现在指向免鉴权的 `/health`（以前指向 `/v1beta/models`，
  配了 `api_keys` 后必然 401，会把容器标成 unhealthy 并刷日志）。
- 这份 compose 默认 `healthcheck.disable: true`，不想要这个信号就保持关闭；
  需要时删掉 `disable` 并启用下面的 `test`。

## 会话保活（防止闲置后掉登录）

每隔 `AISTUDIO_COOKIE_KEEPALIVE_SECONDS`（默认 900 秒，0=关闭）跑一轮，每轮保两个账号：

1. **当前激活账号**：在浏览器里发一次同源 fetch，让 Google 轮换 `__Secure-1PSIDTS`，
   然后立刻回写 `auth.json`（这条最可靠，能拿到真实 Set-Cookie）。
2. **下一个账号**：按注册表顺序轮转，用**该账号自己的 profile** 起一个短命浏览器
   （同一个 CloakBrowser 二进制、同一个 profile 目录），注入现有 cookie 后**模拟一次
   真实登录**（accounts.google.com 引导 → AI Studio），然后把浏览器里**所有域的全量
   cookie 整份覆盖**回 `auth.json`（`replace=True`，旧快照直接丢弃，不再增量合并），
   最后关掉浏览器。TLS 指纹、设备密钥、DBSC 绑定都和正常使用时一致，这是唯一能让
   `__Secure-1PSIDTS` 真正滚动的方式。
   抓到的 jar 会先转成 **Cookie-Editor 的 JSON 导出格式**（`domain` / `expirationDate` /
   `hostOnly` / `httpOnly` / `name` / `path` / `sameSite` / `secure` / `session` /
   `storeId` / `value`），存一份到 `accounts/<id>/cookie-editor-export.json`；
   再把这同一份 JSON 送进面板导入框用的 `parse_cookie_string()`，写盘写的是**导入结果**。
   写盘前还有**回灌自检**：清空浏览器 cookie，只灌导入结果重新登录一遍（等价于
   “Cookie-Editor 导出 → 粘贴导入 → 刷新”）；过不了直接判失败、**绝不写盘**。
   起不来或失败时才回退到 `curl_cffi` 的 HTTP 刷新。

`AISTUDIO_COOKIE_KEEPALIVE_EXTRA`（默认 1）控制每轮额外带几个账号，0 = 只保活跃账号。
`AISTUDIO_COOKIE_KEEPALIVE_BROWSER`（默认 1）控制非活跃账号是否走浏览器；设 0 则只用
HTTP 刷新（省内存，但成功率低）。每轮额外账号会临时多起一个 Chromium（约 200-300MB、
几秒），账号多的话把间隔调大一些。

账号一切换（手动激活或自动轮转）就会立刻补一轮，不等定时器：新账号马上走浏览器级保活，
下一个账号马上做 HTTP 刷新，然后回到原来的间隔节奏。
没有保活的话，闲置几个小时后第一个请求带的是旧时间戳，会撞上 403 或空响应，
表现就是"隔一段时间要重新导一次 cookie"。

```yaml
environment:
  - AISTUDIO_COOKIE_KEEPALIVE_SECONDS=900
  - AISTUDIO_COOKIE_KEEPALIVE_EXTRA=1
  - AISTUDIO_COOKIE_KEEPALIVE_BROWSER=1
```

## 镜像：由本仓库 CI 构建（GHCR）

`.github/workflows/build-personal-image.yml` 会在推送到 `master` / `local` 时，用本仓库的 `Dockerfile` 构建并推送：

```
ghcr.io/sakezerto/aistudio-api:latest
ghcr.io/sakezerto/aistudio-api:<commit-sha>
```

拉取并运行：

```bash
docker pull ghcr.io/sakezerto/aistudio-api:latest
docker compose -f docker-compose.ghcr.yml up -d
```

- 只改文档/compose/`local/**` 不会触发重建（workflow 里有 `paths-ignore`）
- 想手动重建：Actions 页面点 "Run workflow"，或 `gh workflow run build-personal-image.yml -R sakezerto/aistudio-api`
- 镜像只有 `linux/amd64`（CloakBrowser 的 arm64 包也能装，但没在 CI 里构建）
- 包默认跟随仓库可见性，匿名 `docker pull` 已验证可用

## 备注

- `data/`、`.env`、`.venv/` 都在 `.gitignore` 里，不会被提交
- 想备份自己的版本：可以推到自己的私有仓库（`git remote add mine <url> && git push mine local`），不需要 fork 上游

### u/N 槽位回退

`meta.json` 里的 `auth_user` 是**导出那台浏览器**里的账号位置。账号换到本机 profile
后通常就是 0 号，`/u/5/` 会被 Google 判成"这个槽位没有会话"直接踢登录页，看起来像
cookie 失效。`navigate_to_aistudio()` 现在会在「已强制重新注入仍是登录页」之后再跟一次
`u/0`，成功就把实测可用的槽位写回 `meta.json`；`X-Goog-AuthUser` 随之更正。

### 空回判定

流式接口以前只看"零字节"报错，Google 会话异常时回的 `200 + 错误包` 会被当成正常空
回复。现在只要整轮没有任何文本分片就抛错，不再把空响应当成功。

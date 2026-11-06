# 如何上传到 GitHub

> 这是一份从零开始把 Aion Chat 上传到 GitHub 的完整指南。

---

## 一、注册 GitHub 账号（已有可跳过）

1. 打开 https://github.com
2. 点右上角 「Sign up」注册
3. 按提示填写邮箱、用户名、密码，完成注册

---

## 二、安装 Git（已有可跳过）

1. 打开 https://git-scm.com/downloads/win
2. 下载安装包，运行安装
3. 安装过程中所有选项保持默认即可，一路 Next
4. 安装完成后验证：按 `Win + R` 输入 `cmd`，然后输入：
   ```
   git --version
   ```
   看到类似 `git version 2.x.x` 就说明安装成功了

---

## 三、配置 Git 用户信息（首次使用需要）

打开命令提示符（cmd）或 PowerShell，输入（换成你自己的信息）：

```
git config --global user.name "你的GitHub用户名"
git config --global user.email "你的邮箱@example.com"
```

---

## 四、在 GitHub 上创建仓库

1. 登录 GitHub，点右上角 **+** → **New repository**
2. 填写：
   - **Repository name**：`AionsHome`（或你喜欢的名字）
   - **Description**（可选）：`局域网多端同步 AI 聊天系统 + 智能监控`
   - **Public** 或 **Private**：选 **Public**（开源）
   - ⚠️ **不要勾选** 「Add a README file」「Add .gitignore」「Choose a license」（我们本地已经有了）
3. 点 **Create repository**

创建完成后会看到一个页面，上面有推送命令，先别关。

---

## 五、准备本地文件

### ⚠️ 重要：先清理个人数据！

**在上传之前，务必确保已经清理了所有个人隐私数据！**

推荐流程：
1. **复制一份**整个 `AionsHome` 文件夹到一个新位置（比如桌面）
2. 在**复制出来的副本**里双击 **「清理个人数据.bat」**，输入 Y 确认清理
3. 用这个清理后的副本进行下面的 Git 操作

> 或者，如果你信任 `.gitignore` 的配置，也可以直接在原文件夹操作——`.gitignore` 已经排除了所有隐私文件。但为了安全起见，**强烈建议用清理后的副本**。

---

## 六、初始化 Git 并上传

在清理后的 `AionsHome` 文件夹里，右键空白处 → **在终端中打开**（或 **Git Bash Here**）。

然后依次输入以下命令：

```bash
# 1. 初始化 Git 仓库
git init

# 2. 添加所有文件（.gitignore 会自动排除隐私文件）
git add .

# 3. 检查一下将要提交的文件（建议看一眼，确认没有隐私文件）
git status

# 4. 确认没问题后，创建第一次提交
git commit -m "初始提交：Aion Chat 局域网多端同步 AI 聊天系统"

# 5. 设置主分支名称
git branch -M main

# 6. 关联你的 GitHub 仓库（把下面的 URL 换成你自己的）
git remote add origin https://github.com/你的用户名/AionsHome.git

# 7. 推送到 GitHub
git push -u origin main
```

### 🔐 首次推送需要登录

执行 `git push` 时会弹出 GitHub 登录窗口：
- 如果弹出浏览器授权页面，直接点「Authorize」授权即可
- 如果要求输入用户名和密码，**密码需要使用 Personal Access Token（PAT）**，不能用 GitHub 账号密码

#### 如何创建 Personal Access Token：
1. 打开 https://github.com/settings/tokens
2. 点 **Generate new token** → **Generate new token (classic)**
3. Note 随便填个名字，如 `AionsHome`
4. Expiration 选 **No expiration**（永不过期）或选一个时间
5. 勾选 **repo**（仓库完整权限）
6. 点 **Generate token**
7. **立刻复制** 生成的 token（形如 `ghp_xxxx...`），它只显示一次！
8. 在 Git 要求输入密码时，粘贴这个 token

---

## 七、验证上传成功

打开你的 GitHub 仓库页面（`https://github.com/你的用户名/AionsHome`），应该能看到所有文件了。

### 检查清单

请确认以下**隐私文件没有被上传**：

- [ ] `aion-chat/data/chat.db` — 不应出现
- [ ] `aion-chat/data/settings.json` — 不应出现（或内容为 `{}`）
- [ ] `aion-chat/data/worldbook.json` — 不应出现（或内容为空白模板）
- [ ] `aion-chat/data/location_config.json` — 不应出现（或高德 Key 和坐标为空）
- [ ] `aion-chat/data/location_status.json` — 不应出现
- [ ] `aion-chat/data/chats/` 下的 `.md` 文件 — 不应出现
- [ ] `自己看的存档.txt` — 不应出现
- [ ] `MW_RAG_Backup_*.json` — 不应出现
- [ ] `.vscode/` — 不应出现

如果发现有隐私文件被上传了，参考下方「紧急处理」。

---

## 八、后续更新

以后修改了代码想更新到 GitHub：

```bash
git add .
git commit -m "描述你改了什么"
git push
```

---

## ⚠️ 紧急处理：不小心上传了隐私文件

如果发现 API Key 等敏感信息被推送到了 GitHub：

### 1. 立即更换泄露的 Key
- Gemini Key：https://aistudio.google.com/apikey → 删除旧 Key → 创建新 Key
- 硅基流动 Key：https://cloud.siliconflow.cn → 重新生成 Key
- 其他 Key 同理

### 2. 从 Git 历史中彻底移除

```bash
# 从历史中删除敏感文件（例如 settings.json）
git filter-branch --force --index-filter \
  "git rm --cached --ignore-unmatch aion-chat/data/settings.json" \
  --prune-empty -- --all

# 强制推送覆盖远程历史
git push origin --force --all
```

> ⚠️ 仅仅删除文件再提交是**不够的**！Git 历史中仍然保留着旧版本。必须用 `filter-branch` 或 `BFG Repo Cleaner` 彻底清除。

### 3. 确认 .gitignore 已正确配置
确保 `.gitignore` 中包含了所有敏感文件的路径，防止下次再误提交。

---

## 💡 小贴士

- **写个好的 README**：项目里已有 `aion-chat/README.md`，可以在根目录也放一份简化版的项目介绍
- **选择开源协议**：GitHub 仓库页面 → Add file → Create new file → 输入 `LICENSE`，GitHub 会提供协议模板，推荐 MIT License
- **GitHub Pages**：如果以后想做项目主页，可以用 GitHub Pages 功能
- **Star 数**：告诉朋友们来给你点 Star ⭐

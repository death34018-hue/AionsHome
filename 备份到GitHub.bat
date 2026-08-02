@echo off
chcp 65001 >nul
cd /d D:\SJ\AionsHome-main

echo === 初始化 Aion 仓库 ===
git init
git add .
git commit -m "Aion Home - initial backup"

echo === 创建记忆备份目录 ===
mkdir ombre-memories\dynamic 2>nul
mkdir ombre-memories\permanent 2>nul
mkdir ombre-memories\archive 2>nul

echo === 复制 Ombre Brain 记忆 ===
xcopy /E /I /Y "D:\Ombre-Brain-main\buckets\dynamic" "ombre-memories\dynamic"
xcopy /E /I /Y "D:\Ombre-Brain-main\buckets\permanent" "ombre-memories\permanent"
xcopy /E /I /Y "D:\Ombre-Brain-main\buckets\archive" "ombre-memories\archive"

echo === 提交记忆 ===
git add ombre-memories/
git commit -m "Ombre Brain memories backup"

echo === 推送到 GitHub ===
git branch -M main
git remote add origin https://github.com/chronnyx1-QW/Home.git
git push -u origin main

echo === 完成！ ===
pause

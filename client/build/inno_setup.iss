; -*- coding: utf-8 -*-
; Inno Setup 安装脚本（client/build/inno_setup.iss）
;
; 作用：把 PyInstaller onedir 产物（dist/llmBuddy/）+ 精简版推理引擎（engine-runtime/）
;       + 后端源码（serve.py/core/routers）打包为 Windows 一键安装程序。
;
; 布局：
;   {app}\llmBuddy.exe + _internal\    主程序（内嵌 PySide6 GUI + FastAPI 后端）
;   {app}\engine\                         llama.cpp CPU 版运行时（约 45MB）
;   {app}\backend\                        后端源码（高级用户可直接 python serve.py 运行）
;   {app}\models\                         模型目录（空占位，用户在客户端内下载模型）
;   %LOCALAPPDATA%\llmBuddy\config.json   运行时配置（安装目录只读，配置外置）
;
; 用法：先运行 PyInstaller（client/build/build.bat 或 pyinstaller 命令）生成 dist\llmBuddy\，
;       再用 ISCC 编译本文件，产物在 client\build\installer\llmBuddy-Setup-1.0.0.exe

; 产品显示名（快捷方式 / 开始菜单 / 控制面板卸载项都显示这个）
#define MyAppName "llmBuddy"
#define MyAppVersion "1.0.5"
#define MyAppPublisher "llmBuddy"
; 可执行文件名与产品名一致：llmBuddy.exe（内部以 sys.executable 自拉起，无外部依赖旧名）
#define MyAppExeName "llmBuddy.exe"
#define MyAppURL "https://github.com/mark2qq/ebook"
; 产品唯一 GUID：升级识别凭据（不可变更）
#define MyAppGuid "{6C3D5A21-9B4E-4C7A-8F21-5D2E9A1B4C30}"

; ---- 构建机路径（按需修改）----
#define RepoDir "E:\llm-service"
#define DistDir "E:\llm-service\dist\llmBuddy"
#define EngineDir "E:\llm-service\engine-runtime"

[Setup]
; 安装包唯一标识（升级时保持不变：同一 AppId 才能识别为“已安装”并升级覆盖）
AppId={{#MyAppGuid}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; 默认安装目录（用户可改）；管理员权限以便默认装入 Program Files
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
; 红字要求：安装目录用 llmBuddy；升级时不要沿用旧目录（旧目录名 LLMDesktop 已废弃）
UsePreviousAppDir=no
; 产物输出到本脚本同级 installer 目录
OutputDir=installer
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 安装包/卸载图标（与 exe 内图标一致的「llmBuddy 伙伴」图标）
SetupIconFile=..\resources\llmbuddy.ico
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 检测已安装目录中的程序文件并在安装前尝试自动关闭（升级不中断）
CloseApplications=yes
RestartApplications=no
; 卸载信息写入 64 位视图，便于[Code]检测与卸载
UninstallDisplayName={#MyAppName} {#MyAppVersion}
CloseApplicationsFilter=*.exe,*.dll
; 关闭“正在准备安装”进度页以加快安装（小体积安装包可省）
DisableReadyPage=yes

[Languages]
; 简体中文（语言包位于 Inno 安装目录 Languages\ChineseSimplified.isl）
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
; 英文兜底
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full"; Description: "完整安装"
Name: "compact"; Description: "简洁安装"
Name: "custom"; Description: "自定义安装"; Flags: iscustom

[Components]
; 主程序：PyInstaller onedir 产物（内嵌 GUI + 后端服务）
Name: "app"; Description: "主程序（本地大模型服务 + 桌面客户端）"; Types: full compact custom; Flags: fixed
; 推理引擎：llama.cpp CPU 运行时
Name: "engine"; Description: "本地推理引擎（llama.cpp CPU 版，约 45 MB）"; Types: full compact
; 后端源码：便于高级用户排查与二次开发
Name: "backendsrc"; Description: "后端源码（serve.py + core + routers，可直接用 Python 运行）"; Types: full
; 附带一个轻量示例模型？默认不带（模型由客户端内模型库下载）
Name: "models"; Description: "模型文件（由客户端内「模型库」页下载，此处仅创建空目录）"; Types: full compact custom

[Files]
; ---- 主程序（PyInstaller onedir 产物：exe + _internal/）----
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: app

; ---- 推理引擎（CPU 精简版）----
Source: "{#EngineDir}\*"; DestDir: "{app}\engine"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: engine

; ---- 后端源码（可选组件）----
Source: "{#RepoDir}\serve.py"; DestDir: "{app}\backend"; Flags: ignoreversion; Components: backendsrc
Source: "{#RepoDir}\core\*"; DestDir: "{app}\backend\core"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: backendsrc
Source: "{#RepoDir}\routers\*"; DestDir: "{app}\backend\routers"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: backendsrc
Source: "{#RepoDir}\core\catalog_builtin.json"; DestDir: "{app}\backend\core"; Flags: ignoreversion; Components: backendsrc

[Dirs]
; 模型目录占位（首次运行由客户端写入下载的模型）
Name: "{app}\models"; Components: models
; 日志/缓存目录占位
Name: "{app}\backend\logs"
Name: "{app}\backend\cache"

[InstallDelete]
; 旧版本（产品名还是 LLMDesktop 时）留下的快捷方式：升级时清掉，避免桌面出现两个图标
; 注意：以下 LLMDesktop 字样是「旧安装名」，必须保留——正是要清理的对象，勿改成 llmBuddy。
Type: files; Name: "{autodesktop}\LLMDesktop.lnk"
Type: filesandordirs; Name: "{autoprograms}\LLMDesktop"
; 旧安装目录（当时叫 LLMDesktop）：安装目录改名后清掉残留，避免旧文件占空间
; 注意：用户数据（模型/配置/Key）在 %LOCALAPPDATA%\llmBuddy（旧名 LLMDesktop 的目录
; 已由客户端首次启动时自动迁移），不在此安装目录，不受影响。
Type: filesandordirs; Name: "{autopf}\LLMDesktop"

[Icons]
; 显式指定 IconFilename，确保快捷方式使用 exe 内嵌的最新「llmBuddy 伙伴」图标
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Run]
; 安装完成后直接启动（可取消勾选）
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清理安装目录内的运行时日志/缓存。
; 关键：**不删除 %LOCALAPPDATA%\llmBuddy**——那里存着用户下载的模型（数 GB）、
; 配置与已生成的 API Key，卸载/升级都不应清空（历史 bug：卸载后模型和 Key 全丢）。
Type: filesandordirs; Name: "{app}\backend\logs"
Type: filesandordirs; Name: "{app}\backend\cache"

[Code]
// ---------------------------------------------------------------------------
// 升级检测：安装前检查本机是否已安装本程序
//   1) 读取 Uninstall 注册表项（{AppId}_is1）中的 DisplayVersion 与 UninstallString；
//   2) 已安装 → 比较版本：
//        已装版本 < 本包版本 -> 继续安装（Inno 会先卸载旧版再装新版，实现原地升级）
//        已装版本 >= 本包版本 -> 提示并中止（除非用户确认强制重装）
//   3) 未安装 -> 全新安装。
// ---------------------------------------------------------------------------
const
  UninstallKey = 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppGuid}_is1';
  CRLF = #13#10;

var
  InstalledVersion: String;
  IsUpgrade: Boolean;

function GetInstalledVersion(): String;
var
  Found: Boolean;
  V: String;
begin
  Result := '';
  Found := RegQueryStringValue(HKLM, UninstallKey, 'DisplayVersion', V);
  if Found then
    Result := V
  else
  begin
    Found := RegQueryStringValue(HKLM32, UninstallKey, 'DisplayVersion', V);
    if Found then Result := V;
  end;
end;

// 解析版本号第 Idx 段（0 起），越界返回 0
function ParseVersionPart(const S: String; Idx: Integer): Integer;
var
  N, P, I: Integer;
begin
  Result := 0;
  N := 0;
  P := 1;
  for I := 1 to Length(S) do
    if S[I] = '.' then
    begin
      if N = Idx then
      begin
        Result := StrToIntDef(Trim(Copy(S, P, I - P)), 0);
        Exit;
      end;
      Inc(N);
      P := I + 1;
    end;
  if N = Idx then
    Result := StrToIntDef(Trim(Copy(S, P, Length(S) - P + 1)), 0);
end;

// A >= B 返回 True（逐段数值比较，最多 4 段）
function VersionGreaterOrEqual(const A, B: String): Boolean;
var
  I: Integer;
  VA, VB: Integer;
begin
  for I := 0 to 3 do
  begin
    VA := ParseVersionPart(A, I);
    VB := ParseVersionPart(B, I);
    if VA > VB then begin Result := True; Exit; end;
    if VA < VB then begin Result := False; Exit; end;
  end;
  Result := True;
end;

function InitializeSetup(): Boolean;
var
  Installed: String;
  Msg: String;
begin
  Result := True;
  Installed := GetInstalledVersion();
  IsUpgrade := Installed <> '';
  if not IsUpgrade then
  begin
    Log('未检测到已安装版本，执行全新安装');
    Exit;
  end;

  Log('检测到已安装版本 ' + Installed + '，当前安装包版本 ' + '{#MyAppVersion}');
  if VersionGreaterOrEqual(Installed, '{#MyAppVersion}') then
  begin
    Msg := '本机已安装 ' + '{#MyAppName}' + ' ' + Installed + '，版本不低于当前安装包（' + '{#MyAppVersion}' + '）。' +
           CRLF + '如仍要重新安装请选择“是”，选择“否”则退出。';
    if MsgBox(Msg, mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end
  else
  begin
    Msg := '检测到本机已安装 ' + '{#MyAppName}' + ' ' + Installed + '，' +
           CRLF + '是否升级到最新版本 ' + '{#MyAppVersion}' + '？' + CRLF + CRLF +
           '程序文件与快捷方式将被覆盖更新，你已下载的模型文件不会被删除。';
    if MsgBox(Msg, mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and IsUpgrade then
  begin
    Log('升级完成：' + GetInstalledVersion() + ' -> ' + '{#MyAppVersion}');
  end;
end;

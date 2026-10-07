using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using Momoi.Update;

namespace Momoi.Desktop;

internal sealed record ComponentUpdateRequest(string InstallDirectory, int ParentPid, string Kind, string Archive, string Catalog);

internal static class ComponentUpdateWorker
{
    public static void Launch(string install, string archive, string kind, string catalog)
    {
        string directory = Path.Combine(install, "data", "updates", "worker-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        // Run a copy so this process does not lock the shell that it replaces.
        foreach (string file in Directory.GetFiles(install)) File.Copy(file, Path.Combine(directory, Path.GetFileName(file)));
        foreach (string name in new[] { "runtimes", "zh-Hans" })
        {
            string source = Path.Combine(install, name);
            if (Directory.Exists(source)) CopyDirectory(source, Path.Combine(directory, name));
        }
        string request = Path.Combine(directory, "request.json");
        File.WriteAllText(request, JsonSerializer.Serialize(new ComponentUpdateRequest(install, Environment.ProcessId, kind, archive, catalog)));
        var start = new ProcessStartInfo(Path.Combine(directory, "Momoi.exe")) { UseShellExecute = false };
        start.ArgumentList.Add("--install-update"); start.ArgumentList.Add(request);
        _ = Process.Start(start) ?? throw new IOException("无法启动更新程序。");
    }

    public static async Task RunAsync(Application app, string requestPath)
    {
        var view = new StartupView("正在更新…");
        var window = new Window { Title = "Momoi 更新", Width = 540, Height = 360, Content = view, WindowStartupLocation = WindowStartupLocation.CenterScreen };
        window.Closing += (_, e) => e.Cancel = true;
        window.Show();
        string? install = null;
        try
        {
            var request = JsonSerializer.Deserialize<ComponentUpdateRequest>(File.ReadAllText(requestPath)) ?? throw new InvalidDataException("无效更新请求。");
            install = Path.GetFullPath(request.InstallDirectory);
            var catalog = UpdateCatalog.Verify(File.ReadAllBytes(request.Catalog), SignedLatest.EmbeddedPublicKey());
            var artifact = request.Kind == "shell-zip" ? catalog.Shell : request.Kind == "napcat-installer" ? catalog.NapCat : throw new InvalidDataException("未知组件。");
            view.SetDetail("校验中");
            using (var stream = File.OpenRead(request.Archive))
                if (stream.Length != artifact.Size || Convert.ToHexStringLower(SHA256.HashData(stream)) != artifact.Sha256)
                    throw new InvalidDataException("更新文件校验失败。");
            view.SetDetail("等待程序退出");
            try
            {
                using var parent = Process.GetProcessById(request.ParentPid);
                await parent.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(90));
            }
            catch (ArgumentException) { }
            string directory = Path.GetDirectoryName(requestPath)!;
            string script = Path.Combine(directory, "apply.ps1");
            if (request.Kind == "shell-zip")
            {
                view.SetDetail("解压中");
                string payload = Path.Combine(directory, "package");
                await Task.Run(() => ExtractShell(request.Archive, payload));
                File.WriteAllText(script, ShellScript);
                await RunElevatedAsync(script, install, payload);
            }
            else
            {
                view.SetDetail("安装 NapCat / QQ 组件中");
                File.WriteAllText(script, ComponentScript);
                await RunElevatedAsync(script, install, request.Archive);
                string pair = File.ReadAllText(Path.Combine(install, "runtime", "qq-pair", "pair-id.txt")).Trim();
                if (pair != artifact.Id) throw new IOException("已安装组件与签名锁定版本不符。");
                string marker = Path.Combine(install, "data", "updates", "napcat-installed-id.txt");
                File.WriteAllText(marker, artifact.Id);
            }
            view.SetDetail("启动中");
            Process.Start(new ProcessStartInfo(Path.Combine(install, "Momoi.exe")) { UseShellExecute = true });
        }
        catch (Exception error)
        {
            if (install is not null)
            {
                string pending = Path.Combine(install, "data", "updates", "pending.json");
                if (File.Exists(pending)) File.Move(pending, pending + ".failed", true);
            }
            MessageBox.Show("更新未完成：" + error.Message + "\n可重新打开 Momoi；详细信息已保存在更新目录。", "Momoi 更新", MessageBoxButton.OK, MessageBoxImage.Error);
            File.WriteAllText(Path.Combine(Path.GetDirectoryName(requestPath)!, "error.log"), error.ToString());
            if (install is not null && File.Exists(Path.Combine(install, "Momoi.exe")))
                Process.Start(new ProcessStartInfo(Path.Combine(install, "Momoi.exe")) { UseShellExecute = true });
        }
        app.Shutdown();
    }

    internal static void ExtractShell(string archive, string destination)
    {
        using var zip = ZipFile.OpenRead(archive);
        var manifestEntry = zip.GetEntry("shell-manifest.json") ?? throw new InvalidDataException("外壳清单缺失。");
        if (manifestEntry.Length > 4 * 1024 * 1024) throw new InvalidDataException("外壳清单过大。");
        using var manifestStream = manifestEntry.Open();
        using var manifest = JsonDocument.Parse(manifestStream);
        var files = manifest.RootElement.GetProperty("files").EnumerateObject().ToDictionary(p => p.Name, p => p.Value.GetString()!, StringComparer.OrdinalIgnoreCase);
        if (!files.ContainsKey("Momoi.exe") || files.Count > 20000) throw new InvalidDataException("无效外壳清单。");
        long expanded = 0;
        var seen = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in zip.Entries)
        {
            if (entry.FullName == "shell-manifest.json") continue;
            if (!entry.FullName.StartsWith("payload/", StringComparison.Ordinal)) throw new InvalidDataException("未列出的外壳文件。");
            string name = entry.FullName[8..];
            if (name.Contains('\\') || name.Contains(':') || name.Split('/').Any(p => p is "" or "." or ".." || p.EndsWith('.') || p.EndsWith(' ')) ||
                new[] { "data", "runtime", "models", "releases" }.Contains(name.Split('/')[0], StringComparer.OrdinalIgnoreCase) ||
                !seen.Add(name) || !files.ContainsKey(name) || ((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000)
                throw new InvalidDataException("不安全的外壳文件路径。");
            expanded += entry.Length;
            if (expanded > 512L * 1024 * 1024) throw new InvalidDataException("外壳展开体积过大。");
            string path = Path.Combine(destination, "payload", name.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            entry.ExtractToFile(path);
            using var stream = File.OpenRead(path);
            if (Convert.ToHexStringLower(SHA256.HashData(stream)) != files[name]) throw new InvalidDataException("外壳文件校验失败。");
        }
        if (seen.Count != files.Count) throw new InvalidDataException("外壳文件缺失。");
        manifestEntry.ExtractToFile(Path.Combine(destination, "shell-manifest.json"));
    }

    internal static async Task SmokeAsync(string archive)
    {
        string directory = Path.Combine(Path.GetDirectoryName(archive)!, "update-smoke-" + Guid.NewGuid().ToString("N"));
        string package = Path.Combine(directory, "package");
        string install = Path.Combine(directory, "安装测试");
        ExtractShell(archive, package);
        Directory.CreateDirectory(Path.Combine(install, "data"));
        File.WriteAllText(Path.Combine(install, "data", "preserve.txt"), "keep user data");
        File.WriteAllText(Path.Combine(install, "Momoi.exe"), "old shell");
        string script = Path.Combine(directory, "apply.ps1");
        File.WriteAllText(script, ShellScript);
        var start = new ProcessStartInfo("powershell.exe") { UseShellExecute = false };
        foreach (string arg in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-InstallDir", install, "-Package", package }) start.ArgumentList.Add(arg);
        using var child = Process.Start(start) ?? throw new IOException("Failed to start smoke installer");
        await child.WaitForExitAsync();
        if (child.ExitCode != 0 || File.ReadAllText(Path.Combine(install, "data", "preserve.txt")) != "keep user data") throw new IOException("Shell install smoke failed");
        using var actual = File.OpenRead(Path.Combine(install, "Momoi.exe"));
        using var expected = File.OpenRead(Path.Combine(package, "payload", "Momoi.exe"));
        if (!SHA256.HashData(actual).SequenceEqual(SHA256.HashData(expected))) throw new IOException("Shell replacement smoke failed");
        string saved = Directory.GetFiles(Path.Combine(install, "data", "shell-backups"), "Momoi.exe", SearchOption.AllDirectories).Single();
        if (File.ReadAllText(saved) != "old shell") throw new IOException("Shell backup smoke failed");
        // A missing later payload forces rollback after the first file is replaced.
        File.WriteAllText(Path.Combine(package, "shell-manifest.json"), "{\"files\":{\"Momoi.exe\":\"test\",\"missing.dll\":\"test\"}}");
        File.WriteAllText(Path.Combine(install, "Momoi.exe"), "rollback target");
        using var failed = Process.Start(start) ?? throw new IOException("Failed to start rollback smoke");
        await failed.WaitForExitAsync();
        if (failed.ExitCode == 0 || File.ReadAllText(Path.Combine(install, "Momoi.exe")) != "rollback target") throw new IOException("Shell rollback smoke failed");
        File.WriteAllText(archive + ".smoke.json", JsonSerializer.Serialize(new { ok = true, replacement = true, backup = true, rollback = true, unicode = true, userDataPreserved = true }));
        Directory.Delete(directory, true);
    }

    private static void CopyDirectory(string source, string destination)
    {
        Directory.CreateDirectory(destination);
        foreach (string file in Directory.GetFiles(source)) File.Copy(file, Path.Combine(destination, Path.GetFileName(file)));
        foreach (string directory in Directory.GetDirectories(source)) CopyDirectory(directory, Path.Combine(destination, Path.GetFileName(directory)));
    }

    private static async Task RunElevatedAsync(string script, string install, string package)
    {
        // Values come from Windows paths; quotes are forbidden in file names.
        var start = new ProcessStartInfo("powershell.exe") {
            UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden,
            Arguments = $"-NoProfile -ExecutionPolicy Bypass -File \"{script}\" -InstallDir \"{install.TrimEnd('\\')}\" -Package \"{package}\""
        };
        using var child = Process.Start(start) ?? throw new IOException("无法启动安装程序。");
        await child.WaitForExitAsync();
        if (child.ExitCode != 0) throw new IOException("安装程序失败，退出码 " + child.ExitCode);
    }

    private const string ComponentScript = """
param([string]$InstallDir, [string]$Package)
$ErrorActionPreference = 'Stop'
$p = Start-Process $Package -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $InstallDir + '"'), ('/LOG="' + $Package + '.install.log"')) -Wait -PassThru
exit $p.ExitCode
""";
    private const string ShellScript = """
param([string]$InstallDir, [string]$Package)
$ErrorActionPreference = 'Stop'
$payload = Join-Path $Package 'payload'
$manifest = Get-Content (Join-Path $Package 'shell-manifest.json') -Raw | ConvertFrom-Json
$backup = Join-Path $InstallDir ('data\shell-backups\' + [Guid]::NewGuid().ToString('N'))
$changed = New-Object System.Collections.Generic.List[object]
try {
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $source = Join-Path $payload $entry.Name
        $target = Join-Path $InstallDir $entry.Name
        $old = Join-Path $backup $entry.Name
        $existed = Test-Path $target
        if ($existed) {
            New-Item -ItemType Directory -Force (Split-Path $old) | Out-Null
            Copy-Item $target $old -Force
        }
        $changed.Add(@{path=$target;old=$old;existed=$existed})
        New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
        Copy-Item $source $target -Force
    }
} catch {
    $_ | Out-String | Set-Content (Join-Path $Package 'install-error.log')
    foreach ($item in $changed) {
        if ($item.existed) { Copy-Item $item.old $item.path -Force }
        else { Remove-Item $item.path -Force -ErrorAction SilentlyContinue }
    }
    exit 1
}
""";
}

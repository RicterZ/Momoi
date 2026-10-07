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
            var artifact = request.Kind == "shell-zip" ? catalog.Shell : request.Kind == "napcat-installer" ? catalog.NapCat : request.Kind == "asr-installer" && catalog.ASR is not null ? catalog.ASR : throw new InvalidDataException("未知组件。");
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

            if (request.Kind == "shell-zip")
            {
                view.SetDetail("解压中");
                string payload = Path.Combine(directory, "package");
                await Task.Run(() => ExtractShell(request.Archive, payload));
                view.SetDetail("安装外壳中");
                await RunElevatedAsync(requestPath);
            }
            else
            {
                view.SetDetail(request.Kind == "asr-installer" ? "安装本地 ASR 组件中" : "安装 NapCat / QQ 组件中");
                await RunElevatedAsync(requestPath);
                string pair = File.ReadAllText(Path.Combine(install, "runtime", request.Kind == "asr-installer" ? "asr" : "qq-pair", request.Kind == "asr-installer" ? "component-id.txt" : "pair-id.txt")).Trim();
                if (pair != artifact.Id) throw new IOException("已安装组件与签名锁定版本不符。");
                string marker = Path.Combine(install, "data", "updates", request.Kind == "asr-installer" ? "asr-installed-id.txt" : "napcat-installed-id.txt");
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
        ApplyShell(package, install);
        if (File.ReadAllText(Path.Combine(install, "data", "preserve.txt")) != "keep user data") throw new IOException("Shell install smoke failed");
        using (var actual = File.OpenRead(Path.Combine(install, "Momoi.exe")))
        using (var expected = File.OpenRead(Path.Combine(package, "payload", "Momoi.exe")))
            if (!SHA256.HashData(actual).SequenceEqual(SHA256.HashData(expected))) throw new IOException("Shell replacement smoke failed");
        string saved = Directory.GetFiles(Path.Combine(install, "data", "shell-backups"), "Momoi.exe", SearchOption.AllDirectories).Single();
        if (File.ReadAllText(saved) != "old shell") throw new IOException("Shell backup smoke failed");
        // Lock a later destination to force rollback after replacing Momoi.exe.
        File.WriteAllText(Path.Combine(install, "Momoi.exe"), "rollback target");
        string later = Path.Combine(install, "blocked.dll");
        File.WriteAllText(later, "locked original");
        File.WriteAllText(Path.Combine(package, "payload", "blocked.dll"), "new content");
        string hash;
        using (var source = File.OpenRead(Path.Combine(package, "payload", "Momoi.exe"))) hash = Convert.ToHexStringLower(SHA256.HashData(source));
        File.WriteAllText(Path.Combine(package, "shell-manifest.json"), JsonSerializer.Serialize(new { files = new System.Collections.Generic.Dictionary<string,string> { ["Momoi.exe"] = hash, ["blocked.dll"] = Convert.ToHexStringLower(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes("new content"))) } }));
        bool failed = false;
        using (var held = new FileStream(later, FileMode.Open, FileAccess.Read, FileShare.Read))
            try { ApplyShell(package, install); } catch (Exception error) when (error is IOException or UnauthorizedAccessException) { failed = true; }
        if (!failed || File.ReadAllText(Path.Combine(install, "Momoi.exe")) != "rollback target") throw new IOException("Shell rollback smoke failed");
        File.WriteAllText(archive + ".smoke.json", JsonSerializer.Serialize(new { ok = true, replacement = true, backup = true, rollback = true, unicode = true, userDataPreserved = true }));
        Directory.Delete(directory, true);
    }

    private static void CopyDirectory(string source, string destination)
    {
        Directory.CreateDirectory(destination);
        foreach (string file in Directory.GetFiles(source)) File.Copy(file, Path.Combine(destination, Path.GetFileName(file)));
        foreach (string directory in Directory.GetDirectories(source)) CopyDirectory(directory, Path.Combine(destination, Path.GetFileName(directory)));
    }

    private static async Task RunElevatedAsync(string requestPath)
    {
        var start = new ProcessStartInfo(Environment.ProcessPath!) {
            UseShellExecute = true, Verb = "runas",
            Arguments = "--apply-component-elevated \"" + requestPath + "\""
        };
        using var child = Process.Start(start) ?? throw new IOException("无法启动安装程序。");
        await child.WaitForExitAsync();
        if (child.ExitCode != 0)
        {
            string errorPath = Path.Combine(Path.GetDirectoryName(requestPath)!, "apply-error.log");
            throw new IOException(File.Exists(errorPath) ? File.ReadAllText(errorPath) : "安装程序失败，退出码 " + child.ExitCode);
        }
    }

    internal static async Task<int> ApplyElevatedAsync(string requestPath)
    {
        try
        {
            var request = JsonSerializer.Deserialize<ComponentUpdateRequest>(File.ReadAllText(requestPath)) ?? throw new InvalidDataException("无效更新请求。");
            string install = Path.GetFullPath(request.InstallDirectory);
            var catalog = UpdateCatalog.Verify(File.ReadAllBytes(request.Catalog), SignedLatest.EmbeddedPublicKey());
            var artifact = request.Kind == "shell-zip" ? catalog.Shell : request.Kind == "napcat-installer" ? catalog.NapCat : request.Kind == "asr-installer" && catalog.ASR is not null ? catalog.ASR : throw new InvalidDataException("未知组件。");
            using (var stream = File.OpenRead(request.Archive))
                if (stream.Length != artifact.Size || Convert.ToHexStringLower(SHA256.HashData(stream)) != artifact.Sha256) throw new InvalidDataException("更新文件校验失败。");
            if (request.Kind == "shell-zip")
            {
                string package = Path.Combine(Path.GetDirectoryName(requestPath)!, "verified-" + Guid.NewGuid().ToString("N"));
                try { ExtractShell(request.Archive, package); ApplyShell(package, install); }
                finally { if (Directory.Exists(package)) Directory.Delete(package, true); }
            }
            else
            {
                var start = new ProcessStartInfo(request.Archive) { UseShellExecute = false };
                foreach (string arg in new[] { "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=" + install, "/LOG=" + request.Archive + ".install.log" }) start.ArgumentList.Add(arg);
                using var child = Process.Start(start) ?? throw new IOException("无法启动 QQ 组件安装程序。");
                await child.WaitForExitAsync();
                if (child.ExitCode != 0) throw new IOException("QQ 组件安装失败，退出码 " + child.ExitCode);
                if (File.ReadAllText(Path.Combine(install, "runtime", request.Kind == "asr-installer" ? "asr" : "qq-pair", request.Kind == "asr-installer" ? "component-id.txt" : "pair-id.txt")).Trim() != artifact.Id) throw new IOException("已安装 QQ 组件与签名锁定版本不符。");
            }
            return 0;
        }
        catch (Exception error)
        {
            File.WriteAllText(Path.Combine(Path.GetDirectoryName(requestPath)!, "apply-error.log"), error.ToString());
            return 1;
        }
    }

    internal static void ApplyShell(string package, string install)
    {
        using var manifest = JsonDocument.Parse(File.ReadAllText(Path.Combine(package, "shell-manifest.json")));
        var files = manifest.RootElement.GetProperty("files").EnumerateObject().ToArray();
        // Check every source before touching the installation.
        foreach (var entry in files)
        {
            string name = entry.Name;
            if (name.Contains('\\') || name.Contains(':') || name.Split('/').Any(part => part is "" or "." or ".." || part.EndsWith('.') || part.EndsWith(' ')) ||
                new[] { "data", "runtime", "models", "releases" }.Contains(name.Split('/')[0], StringComparer.OrdinalIgnoreCase)) throw new InvalidDataException("不安全的外壳文件路径。");
            using var source = File.OpenRead(Path.Combine(package, "payload", name.Replace('/', Path.DirectorySeparatorChar)));
            if (Convert.ToHexStringLower(SHA256.HashData(source)) != entry.Value.GetString()) throw new InvalidDataException("外壳文件校验失败。");
        }
        string backup = Path.Combine(install, "data", "shell-backups", Guid.NewGuid().ToString("N"));
        var changed = new System.Collections.Generic.List<(string Target, string Old, bool Existed)>();
        try
        {
            foreach (var entry in files)
            {
                string name = entry.Name.Replace('/', Path.DirectorySeparatorChar);
                string target = Path.Combine(install, name), old = Path.Combine(backup, name);
                bool existed = File.Exists(target);
                if (existed) { Directory.CreateDirectory(Path.GetDirectoryName(old)!); File.Copy(target, old); }
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                string temporary = target + ".update-" + Guid.NewGuid().ToString("N");
                try
                {
                    File.Copy(Path.Combine(package, "payload", name), temporary);
                    File.Move(temporary, target, true);
                    changed.Add((target, old, existed));
                }
                finally { if (File.Exists(temporary)) File.Delete(temporary); }
            }
            File.WriteAllText(Path.Combine(backup, "backup.json"), JsonSerializer.Serialize(changed.Select(item => new { path = item.Target, old = item.Old, existed = item.Existed })));
        }
        catch (Exception installError)
        {
            var errors = new System.Collections.Generic.List<Exception> { installError };
            foreach (var item in changed.AsEnumerable().Reverse())
                try { if (item.Existed) File.Copy(item.Old, item.Target, true); else File.Delete(item.Target); }
                catch (Exception restoreError) { errors.Add(restoreError); }
            if (errors.Count > 1) throw new AggregateException("外壳替换失败，部分文件需要从备份恢复。", errors);
            throw;
        }
    }
}

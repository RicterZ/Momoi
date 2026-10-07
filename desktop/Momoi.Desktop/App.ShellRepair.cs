using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using Momoi.Update;

namespace Momoi.Desktop;

public partial class App
{
    private async Task RepairShellAsync()
    {
        shellRoot = new Grid();
        shellRoot.Children.Add(new StartupView("正在检查更新…"));
        panel = new Window { Title = "Momoi — 外壳更新", Width = 980, Height = 650, Content = shellRoot, WindowStartupLocation = WindowStartupLocation.CenterScreen };
        panel.Closing += HideOnClose;
        panel.Show();
        try
        {
            string install = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Momoi");
            var choose = new Microsoft.Win32.OpenFileDialog { Title = "选择已安装的 Momoi.exe", FileName = Path.Combine(install, "Momoi.exe"), Filter = "Momoi 程序|Momoi.exe", CheckFileExists = true };
            if (choose.ShowDialog(panel) != true) return;
            install = Path.GetDirectoryName(choose.FileName)!;
            if (string.Equals(install.TrimEnd(Path.DirectorySeparatorChar), AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase)) throw new IOException("请从下载的更新目录运行更新器，选择已安装的 Momoi。");
            byte[] envelope = await UpdateCatalog.FetchEnvelopeAsync(lifetime.Token);
            var catalog = UpdateCatalog.Verify(envelope, SignedLatest.EmbeddedPublicKey());
            if (!await ShowUpdatePromptAsync("更新 Momoi 外壳", "将下载并验证最新外壳，关闭正在运行的 Momoi 后替换。配置和数据将保留。", "更新外壳", "取消")) return;
            string directory = Path.Combine(AppContext.BaseDirectory, "repair-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            string catalogPath = Path.Combine(directory, "catalog.json");
            File.WriteAllBytes(catalogPath, envelope);
            string archive = await UpdateCatalog.DownloadAsync(catalog.Shell, directory, new Progress<UpdateProgress>(_ => { }), lifetime.Token);
            int parentPid = int.MaxValue;
            foreach (var process in Process.GetProcessesByName("Momoi")) using (process)
            {
                try
                {
                    if (string.Equals(process.MainModule?.FileName, Path.Combine(install, "Momoi.exe"), StringComparison.OrdinalIgnoreCase)) { parentPid = process.Id; break; }
                }
                catch (Exception error) when (error is InvalidOperationException or System.ComponentModel.Win32Exception) { }
            }
            if (parentPid != int.MaxValue)
            {
                using var pipe = new NamedPipeClientStream(".", InstanceName, PipeDirection.Out);
                await pipe.ConnectAsync(5000);
                using var writer = new StreamWriter(pipe) { AutoFlush = true };
                await writer.WriteLineAsync("exit");
            }
            string request = Path.Combine(directory, "request.json");
            File.WriteAllText(request, JsonSerializer.Serialize(new ComponentUpdateRequest(install, parentPid, "shell-zip", archive, catalogPath)));
            panel.Hide();
            await ComponentUpdateWorker.RunAsync(this, request);
        }
        catch (Exception error) { await ShowUpdatePromptAsync("外壳更新未完成", error.Message); }
        finally { exiting = true; Shutdown(); }
    }
}

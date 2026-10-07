using System.Diagnostics;
using System.Net.Http;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Momoi.Desktop;
using Momoi.Update;

namespace Momoi.Installer;

public partial class App : Application
{
    private Window window = null!;
    private Grid surface = null!;
    private TextBox directory = null!;
    private TextBlock message = null!;
    private Button installButton = null!, chooseButton = null!, cancelButton = null!;
    private StartupView? loading;
    private CheckBox cable = null!;
    private CancellationTokenSource? operation;
    private bool installing, installed;
    private readonly string cache = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Momoi", "Installer");
    private string log = "";
    private string selfDirectory = "";

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        selfDirectory = Path.GetDirectoryName(Environment.ProcessPath)!;
        if (e.Args.Length == 2 && e.Args[0] == "--download-check")
        {
            try { await DownloadCheckAsync(e.Args[1]); Shutdown(0); }
            catch (Exception error) { Directory.CreateDirectory(e.Args[1]); File.WriteAllText(Path.Combine(e.Args[1], "error.log"), error.ToString()); Shutdown(1); }
            return;
        }
        Directory.CreateDirectory(cache);
        log = Path.Combine(cache, "installer-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log");
        CreateWindow();
        window.Show();
        if (e.Args.Length == 2 && e.Args[0] == "--ui-preview")
        {
            await Task.Delay(250);
            Directory.CreateDirectory(e.Args[1]);
            SavePreview(surface, Path.Combine(e.Args[1], "online-installer.png"));
            Shutdown(0);
        }
        else if (e.Args.Length == 2 && e.Args[0] == "--install-check")
        {
            directory.Text = e.Args[1];
            await InstallAsync();
            if (installed) File.WriteAllText(Path.Combine(e.Args[1], "online-install-check.json"), "{\"ok\":true,\"nativeInstaller\":true,\"runtimeVerified\":true,\"qqPairVerified\":true}");
            Shutdown(installed ? 0 : 1);
        }
    }

    private static void SavePreview(FrameworkElement element, string path)
    {
        element.UpdateLayout();
        var image = new RenderTargetBitmap((int)element.ActualWidth, (int)element.ActualHeight, 96, 96, PixelFormats.Pbgra32);
        image.Render(element);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(image));
        using var output = File.Create(path); encoder.Save(output);
    }

    private void CreateWindow()
    {
        surface = new Grid { Background = (Brush)FindResource("Canvas") };
        var content = new StackPanel { Margin = new Thickness(32), VerticalAlignment = VerticalAlignment.Center };
        content.Children.Add(new TextBlock { Text = "MOMOI  /  在线安装", Foreground = (Brush)FindResource("Pink"), FontWeight = FontWeights.SemiBold });
        content.Children.Add(new TextBlock { Text = "安装 Momoi", FontSize = 28, FontWeight = FontWeights.Bold, Margin = new Thickness(0, 14, 0, 16) });
        content.Children.Add(new TextBlock { Text = "自动下载所需运行组件、BGE 模型和 QQ。已有本地组件将优先使用，安装后无需配置运行环境。", FontSize = 14, TextWrapping = TextWrapping.Wrap, Foreground = (Brush)FindResource("Muted"), Margin = new Thickness(0, 0, 0, 26) });
        content.Children.Add(new TextBlock { Text = "安装位置", FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 8) });
        var row = new Grid(); row.ColumnDefinitions.Add(new ColumnDefinition()); row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        directory = new TextBox { Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Momoi"), Padding = new Thickness(12), BorderBrush = (Brush)FindResource("Line"), Background = Brushes.White, VerticalContentAlignment = VerticalAlignment.Center };
        chooseButton = Button("选择", false); chooseButton.Margin = new Thickness(12, 0, 0, 0); Grid.SetColumn(chooseButton, 1);
        chooseButton.Click += (_, _) => { var chooser = new Microsoft.Win32.OpenFolderDialog { Title = "选择安装目录", InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles) }; if (chooser.ShowDialog(window) == true) directory.Text = chooser.FolderName; };
        row.Children.Add(directory); row.Children.Add(chooseButton); content.Children.Add(row);
        message = new TextBlock { Text = "安装期间需要联网，并可能请求管理员授权。配置和用户数据保存在安装目录的 data 文件夹。", TextWrapping = TextWrapping.Wrap, Foreground = (Brush)FindResource("Muted"), Margin = new Thickness(0, 16, 0, 22), LineHeight = 22 };
        content.Children.Add(message);
        cable = new CheckBox { Content = "安装 VB-CABLE（语音通话组件；已有 Steam 音频设备可跳过）", IsChecked = false, Margin = new Thickness(0, 0, 0, 8) };
        content.Children.Add(cable);
        content.Children.Add(new TextBlock { Text = "VB-Audio · www.vb-cable.com · Donationware，欢迎捐赠支持。", Foreground = (Brush)FindResource("Muted"), Margin = new Thickness(0, 0, 0, 8) });
        var audioList = new TextBlock { Text = string.Join("\n", AudioDevicePreference.Installed()), TextWrapping = TextWrapping.Wrap };
        content.Children.Add(new ScrollViewer { Content = audioList, Height = 90, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(0, 0, 0, 16) });
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        cancelButton = Button("关闭", false); cancelButton.Margin = new Thickness(0, 0, 12, 0);
        installButton = Button("安装", true);
        cancelButton.Click += (_, _) => { if (operation is not null) operation.Cancel(); else Shutdown(); };
        installButton.Click += async (_, _) => { if (installed) { if (LaunchInstalled()) Shutdown(); } else await InstallAsync(); };
        buttons.Children.Add(cancelButton); buttons.Children.Add(installButton); content.Children.Add(buttons);
        surface.Children.Add(content);
        window = new Window { Title = "Momoi 在线安装", Width = 640, Height = 690, ResizeMode = ResizeMode.NoResize, Content = surface, WindowStartupLocation = WindowStartupLocation.CenterScreen, FontFamily = new FontFamily("Segoe UI Variable, Segoe UI, DengXian"), FontSize = 13, Foreground = (Brush)FindResource("Ink"), Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Assets/momoi.png")) };
        window.Closing += (_, e) => { if (operation is not null) { e.Cancel = true; if (!installing) operation.Cancel(); } else Shutdown(); };
    }
    private Button Button(string label, bool primary)
    {
        var button = new Button { Content = label, MinWidth = 100, Style = (Style)FindResource("MomoiButton") };
        if (primary) { button.Background = (Brush)FindResource("Pink"); button.BorderBrush = button.Background; button.Foreground = Brushes.White; }
        return button;
    }
    private void Log(string value) => File.AppendAllText(log, $"{DateTimeOffset.Now:O} {value}{Environment.NewLine}");
    private void SetBusy(bool busy)
    {
        installButton.IsEnabled = chooseButton.IsEnabled = directory.IsEnabled = !busy;
        cancelButton.Content = busy ? "取消下载" : "关闭";
        if (busy)
        {
            loading = new StartupView("正在安装…") { Margin = new Thickness(0, 0, 0, 70) };
            surface.Children[0].Visibility = Visibility.Hidden;
            surface.Children.Add(loading);
            // Keep a separate cancel button reachable while the welcome form is covered.
            surface.Children.Add(new Border { Child = new Button { Content = "取消下载", Style = (Style)FindResource("MomoiButton"), Command = new CancelCommand(() => operation?.Cancel(), () => !installing) }, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Bottom, Margin = new Thickness(0, 0, 0, 30) });
        }
        else
        {
            while (surface.Children.Count > 1) surface.Children.RemoveAt(1);
            loading = null; surface.Children[0].Visibility = Visibility.Visible;
        }
    }
    private async Task InstallAsync()
    {
        operation = new CancellationTokenSource(TimeSpan.FromHours(2));
        using var single = new Mutex(false, @"Local\Momoi.OnlineInstaller.v1");
        bool owns = false;
        try
        {
            try { owns = single.WaitOne(0); } catch (AbandonedMutexException) { owns = true; }
            if (!owns) throw new IOException("另一个 Momoi 在线安装器正在运行，请先完成该安装。");
            string requested = directory.Text.Trim();
            if (!Path.IsPathFullyQualified(requested)) throw new IOException("请选择完整的本机安装路径。");
            string destination = Path.GetFullPath(requested).TrimEnd(Path.DirectorySeparatorChar);
            if (!Path.IsPathFullyQualified(destination) || destination.Length < 4 || destination.Contains('"') || destination.StartsWith(@"\\")) throw new IOException("请选择本机有效的安装目录。");
            directory.Text = destination;
            SetBusy(true); Log("Fetching signed installation catalog");
            byte[] envelope = await InstallCatalog.FetchEnvelopeAsync(operation.Token);
            InstallCatalog catalog = InstallCatalog.Verify(envelope, SignedLatest.EmbeddedPublicKey());
            Log($"Installation set {catalog.Version}; runtime {catalog.RuntimeId}; QQ pair {catalog.QQPairId}");
            using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = Timeout.InfiniteTimeSpan };
            var downloader = new ArtifactDownloader(client);
            string component = "核心组件";
            var progress = new Progress<UpdateProgress>(value => { string detail = component + " · " + value.Phase; if (value.Total > 0) detail += $" · {Math.Clamp(value.Completed * 100 / value.Total, 0, 100)}% · {value.Completed / 1048576.0:F1} / {value.Total / 1048576.0:F1} MiB"; loading?.SetDetail(detail); });
            string[] local = [selfDirectory, Path.Combine(selfDirectory, "components")];
            loading?.SetDetail("准备核心运行组件");
            string core = await downloader.DownloadAsync(catalog.Core, cache, local, progress, operation.Token);
            component = "QQ 组件";
            string qq = await downloader.DownloadAsync(catalog.NapCat, cache, local, progress, operation.Token);
            string components = Path.Combine(Path.GetDirectoryName(core)!, "components");
            Directory.CreateDirectory(components);
            string pair = Path.Combine(components, Path.GetFileName(qq)); File.Copy(qq, pair, true);
            // Keep Microsoft files from an offline distribution available to the native installer.
            string prerequisites = Path.Combine(selfDirectory, "components", "prerequisites");
            if (Directory.Exists(prerequisites))
            {
                string target = Path.Combine(components, "prerequisites"); Directory.CreateDirectory(target);
                foreach (string file in Directory.GetFiles(prerequisites)) File.Copy(file, Path.Combine(target, Path.GetFileName(file)), true);
            }
            if (!await ArtifactDownloader.MatchesAsync(core, catalog.Core, operation.Token) || !await ArtifactDownloader.MatchesAsync(pair, catalog.NapCat, operation.Token)) throw new InvalidDataException("安装文件校验失败。");
            operation.Token.ThrowIfCancellationRequested();
            installing = true; System.Windows.Input.CommandManager.InvalidateRequerySuggested(); loading?.SetDetail("安装中 · 请确认 Windows 管理员授权");
            string nativeLog = Path.Combine(cache, "native-install-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log");
            var start = new ProcessStartInfo(core) { UseShellExecute = true, Verb = "runas", Arguments = $"/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=\"{(cable.IsChecked == true ? "desktopicon,vbcable" : "desktopicon")}\" /DIR=\"{destination}\" /LOG=\"{nativeLog}\"" };
            Log("Requesting elevated native installer launch: " + core);
            using var child = Process.Start(start) ?? throw new IOException("无法启动安装程序。");
            Log($"Native installer started PID={child.Id}; log={nativeLog}");
            loading?.SetDetail("安装中 · 正在安装运行组件（无需再次授权）");
            await child.WaitForExitAsync();
            Log("Native installer exit code " + child.ExitCode);
            if (child.ExitCode is not (0 or 3010)) throw new IOException($"安装未完成（退出码 {child.ExitCode}）。日志：{nativeLog}");
            using var runtime = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(destination, "runtime", "runtime.json")));
            if (runtime.RootElement.GetProperty("runtime_id").GetString() != catalog.RuntimeId || File.ReadAllText(Path.Combine(destination, "runtime", "qq-pair", "pair-id.txt")).Trim() != catalog.QQPairId || !File.Exists(Path.Combine(destination, "Momoi.exe"))) throw new IOException("安装后的运行组件版本不匹配，请重试。");
            installed = true; installButton.Content = "打开 Momoi";
            message.Text = child.ExitCode == 3010 ? "Momoi 已安装。Windows 前置组件需要重启系统，请重启后打开 Momoi。" : "Momoi 已安装，可以打开并完成首次设置。之后从应用内检查更新即可。";
            Log("Installation verified");
        }
        catch (OperationCanceledException) { Log("Download canceled; resumable partials retained"); message.Text = "下载已取消，已下载内容保留。点击安装可继续。"; }
        catch (Exception error) { Log(error.ToString()); message.Text = "安装未完成：" + error.Message + "\n可点击重试。日志：" + log; installButton.Content = "重试"; }
        finally { installing = false; operation.Dispose(); operation = null; SetBusy(false); if (owns) single.ReleaseMutex(); }
    }
    private bool LaunchInstalled()
    {
        try { Process.Start(new ProcessStartInfo(Path.Combine(directory.Text, "Momoi.exe")) { UseShellExecute = true }); return true; }
        catch (Exception error) { message.Text = "打开失败：" + error.Message; return false; }
    }
    private static async Task DownloadCheckAsync(string directory)
    {
        Directory.CreateDirectory(directory);
        var catalog = InstallCatalog.Verify(await InstallCatalog.FetchEnvelopeAsync(CancellationToken.None), SignedLatest.EmbeddedPublicKey());
        using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = Timeout.InfiniteTimeSpan };
        var downloader = new ArtifactDownloader(client);
        var progress = new Progress<UpdateProgress>(_ => { });
        foreach (var artifact in new[] { catalog.Core, catalog.NapCat })
        {
            string path = await downloader.DownloadAsync(artifact, directory, Array.Empty<string>(), progress, CancellationToken.None);
            if (!await ArtifactDownloader.MatchesAsync(path, artifact, CancellationToken.None)) throw new IOException("Downloaded component checksum mismatch");
        }
        File.WriteAllText(Path.Combine(directory, "download-check.json"), JsonSerializer.Serialize(new { ok = true, version = catalog.Version, signature = true, hashes = true }));
    }
    private sealed class CancelCommand(Action execute, Func<bool> enabled) : System.Windows.Input.ICommand
    {
        public bool CanExecute(object? parameter) => enabled();
        public void Execute(object? parameter) => execute();
        public event EventHandler? CanExecuteChanged { add => System.Windows.Input.CommandManager.RequerySuggested += value; remove => System.Windows.Input.CommandManager.RequerySuggested -= value; }
    }
}

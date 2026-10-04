using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Imaging;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Forms = System.Windows.Forms;
using Drawing = System.Drawing;
using Momoi.Update;

namespace Momoi.Desktop;

public partial class App : Application
{
    private const string InstanceName = "Momoi.Desktop.v1";
    private readonly CancellationTokenSource lifetime = new();
    private Mutex? instance;
    private bool ownsInstance, exiting, updateBusy, switching;
    private Forms.ToolStripItem? updateMenu;
    private ReleaseStore? releases;
    private CodeRelease? currentRelease;
    private string? authScript;
    private string? dashboardUrl;
    private Window? panel;
    private Forms.NotifyIcon? tray;
    private Drawing.Icon? trayImage;
    private BackendHost? backend;
    private WebView2? browser;
    private Task? startup;
    private Task? pipeListener;
    private Task? updateTask;
    private System.Windows.Threading.DispatcherTimer? updateTimer;
    private string workspace = "";

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        instance = new Mutex(false, @"Local\" + InstanceName);
        try { ownsInstance = instance.WaitOne(0); }
        catch (AbandonedMutexException) { ownsInstance = true; }
        if (!ownsInstance)
        {
            try
            {
                using var pipe = new NamedPipeClientStream(".", InstanceName, PipeDirection.Out);
                await pipe.ConnectAsync(3000);
                using var writer = new StreamWriter(pipe) { AutoFlush = true };
                await writer.WriteLineAsync(Array.IndexOf(e.Args, "--shutdown") >= 0 ? "exit" : "show");
            }
            catch (Exception error) when (error is IOException or TimeoutException)
            { MessageBox.Show("Momoi 已在启动或运行中，请通过托盘打开面板。", "Momoi"); }
            Shutdown();
            return;
        }
        if (Array.IndexOf(e.Args, "--shutdown") >= 0) { Shutdown(); return; }
        workspace = Path.Combine(AppContext.BaseDirectory, "data");
        try
        {
            Directory.CreateDirectory(workspace);
            string probe = Path.Combine(workspace, ".write-check-" + Guid.NewGuid().ToString("N"));
            using (var stream = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1, FileOptions.DeleteOnClose)) { }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            MessageBox.Show($"无法写入配置目录：{workspace}\n请通过安装程序修复目录权限，或安装到有写入权限的目录。\n{error.Message}", "Momoi", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown();
            return;
        }
        var resource = GetResourceStream(new Uri("pack://application:,,,/Assets/momoi.ico"))!;
        trayImage = new Drawing.Icon(resource.Stream);
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("打开面板", null, (_, _) => Dispatcher.BeginInvoke(ShowPanel));
        menu.Items.Add("配置目录", null, (_, _) => Dispatcher.BeginInvoke(() =>
        {
            Directory.CreateDirectory(workspace);
            Process.Start(new ProcessStartInfo(workspace) { UseShellExecute = true });
        }));
        updateMenu = menu.Items.Add("检查更新", null, (_, _) => Dispatcher.BeginInvoke(() => updateTask = UpdateAsync()));
        menu.Items.Add("退出程序", null, (_, _) => Dispatcher.BeginInvoke(() => _ = ExitAsync()));
        tray = new Forms.NotifyIcon { Icon = trayImage, Text = "Momoi", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => Dispatcher.BeginInvoke(ShowPanel);
        panel = new Window
        {
            Title = "Momoi", Width = 1240, Height = 850, MinWidth = 800, MinHeight = 600,
            Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Assets/momoi.png")),
            Content = new TextBlock { Text = "正在启动 Momoi 与本地 BGE 编码器…", Margin = new Thickness(32), FontSize = 18 },
        };
        MainWindow = panel;
        panel.Closing += HideOnClose;
        panel.Show();
        pipeListener = ListenForActivationAsync();
        startup = StartServicesAsync();
        await startup;
    }

    private async Task ListenForActivationAsync()
    {
        while (!lifetime.IsCancellationRequested)
        {
            try
            {
                using var pipe = new NamedPipeServerStream(InstanceName, PipeDirection.In, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync(lifetime.Token);
                using var reader = new StreamReader(pipe);
                string? command = await reader.ReadLineAsync(lifetime.Token);
                if (command == "show") _ = Dispatcher.BeginInvoke(ShowPanel);
                else if (command == "exit") _ = Dispatcher.BeginInvoke(() => _ = ExitAsync());
            }
            catch (OperationCanceledException) { break; }
            catch (IOException) { }
        }
    }

    private async Task StartServicesAsync()
    {
        try
        {
            // Fail early with a useful installer message if WebView2 is missing.
            CoreWebView2Environment.GetAvailableBrowserVersionString();
            releases = new ReleaseStore(AppContext.BaseDirectory, workspace);
            currentRelease = releases.Initialize();
            backend = new BackendHost(workspace);
            var ready = await backend.StartAsync(workspace, currentRelease, lifetime.Token);
            if (exiting) return;
            await LoadDashboardAsync(ready);
            _ = WatchBackendAsync(backend);
            updateTimer = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromSeconds(10) };
            updateTimer.Tick += (_, _) => { updateTimer.Stop(); updateTask = UpdateAsync(quiet: true); };
            updateTimer.Start();
        }
        catch (OperationCanceledException) when (exiting) { }
        catch (Exception error)
        {
            if (!exiting)
            {
                MessageBox.Show($"Momoi 启动失败：{error.Message}\n\n日志目录：{Path.Combine(workspace, "logs")}", "Momoi", MessageBoxButton.OK, MessageBoxImage.Error);
                // Avoid waiting on this startup task from within itself.
                _ = Dispatcher.BeginInvoke(() => _ = ExitAsync());
            }
        }
    }

    private async Task LoadDashboardAsync(BackendReady ready)
    {
        if (browser is null)
        {
            browser = new WebView2();
            panel!.Content = browser;
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: Path.Combine(workspace, "webview"));
            await browser.EnsureCoreWebView2Async(environment);
            browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
            browser.CoreWebView2.Settings.IsWebMessageEnabled = false;
            browser.CoreWebView2.NavigationStarting += (_, args) =>
            {
                if (args.Uri == "about:blank") return;
                if (!Uri.TryCreate(args.Uri, UriKind.Absolute, out var uri) || uri.GetLeftPart(UriPartial.Authority) != dashboardUrl)
                { args.Cancel = true; OpenExternal(args.Uri); }
            };
            browser.CoreWebView2.NewWindowRequested += (_, args) => { args.Handled = true; OpenExternal(args.Uri); };
        }
        dashboardUrl = ready.Url;
        if (authScript is not null) browser.CoreWebView2.RemoveScriptToExecuteOnDocumentCreated(authScript);
        authScript = await browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
            $"if (location.origin === {JsonSerializer.Serialize(ready.Url)}) localStorage.setItem('momoi-dashboard-token', {JsonSerializer.Serialize(ready.Token)});");
        browser.Source = new Uri(ready.Url);
    }

    private async Task UpdateAsync(bool quiet = false)
    {
        if (updateBusy || exiting || releases is null || currentRelease is null || backend is null) return;
        updateBusy = true;
        bool installationRequested = false;
        if (updateMenu is not null) updateMenu.Enabled = false;
        try
        {
            tray!.Text = "Momoi — 正在检查更新";
            LatestRelease latest = await SignedLatest.FetchAsync(lifetime.Token);
            if (latest.ReleaseId == currentRelease.Manifest.ReleaseId)
            { if (!quiet) MessageBox.Show("当前已是最新发布版本。", "Momoi"); return; }
            if (MessageBox.Show($"发现新版本 {latest.Version}（当前 {currentRelease.Manifest.Version}）。\n是否下载安装？安装完成后会重启后台并刷新面板。", "Momoi 更新", MessageBoxButton.YesNo, MessageBoxImage.Information) != MessageBoxResult.Yes) return;
            installationRequested = true;
            tray!.Text = "Momoi — 正在下载并验证更新";
            CodeRelease next = await releases.DownloadAsync(latest, lifetime.Token);
            tray.Text = "Momoi — 正在安装更新";
            switching = true;
            CodeRelease previous = currentRelease;
            string snapshot = Path.Combine(workspace, "update-backups", DateTime.UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N"));
            browser?.CoreWebView2.Navigate("about:blank");
            await backend.DisposeAsync();
            backend = null;
            try
            {
                await BackendHost.MaintenanceAsync(workspace, previous, snapshot, restore: false);
                releases.Activate(next);
                backend = new BackendHost(workspace);
                var ready = await backend.StartAsync(workspace, next, lifetime.Token);
                currentRelease = next;
                await LoadDashboardAsync(ready);
                _ = WatchBackendAsync(backend);
                ShowPanel();
                tray!.ShowBalloonTip(3000, "Momoi 更新完成", $"已安装版本 {next.Manifest.Version}，后台已重启。", Forms.ToolTipIcon.Info);
            }
            catch (Exception updateError)
            {
                if (backend is not null) { await backend.DisposeAsync(); backend = null; }
                // Restore data as well as code: startup may have run SQLite migrations.
                if (File.Exists(Path.Combine(snapshot, "snapshot.json")))
                    await BackendHost.MaintenanceAsync(workspace, previous, snapshot, restore: true);
                releases.Activate(previous);
                currentRelease = previous;
                if (!exiting)
                {
                    backend = new BackendHost(workspace);
                    var ready = await backend.StartAsync(workspace, previous, lifetime.Token);
                    await LoadDashboardAsync(ready);
                    _ = WatchBackendAsync(backend);
                    MessageBox.Show($"更新未能启动，已恢复原版本与数据。\n{updateError.Message}", "Momoi", MessageBoxButton.OK, MessageBoxImage.Warning);
                }
            }
        }
        catch (OperationCanceledException) when (exiting) { }
        catch (Exception error) { if (!exiting && (!quiet || installationRequested)) MessageBox.Show($"更新失败：{error.Message}", "Momoi", MessageBoxButton.OK, MessageBoxImage.Error); }
        finally
        {
            switching = false;
            updateBusy = false;
            if (updateMenu is not null) updateMenu.Enabled = true;
            if (tray is not null && !exiting) tray.Text = "Momoi";
        }
    }

    private static void OpenExternal(string url)
    {
        if (Uri.TryCreate(url, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https")
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }

    private async Task WatchBackendAsync(BackendHost watched)
    {
        await watched.Completion;
        if (!exiting && !switching)
        {
            MessageBox.Show($"后台程序已停止，请查看日志后重新启动。\n{Path.Combine(workspace, "logs")}", "Momoi", MessageBoxButton.OK, MessageBoxImage.Error);
            await ExitAsync();
        }
    }

    private void ShowPanel()
    {
        if (panel is null || exiting) return;
        panel.Show();
        if (panel.WindowState == WindowState.Minimized) panel.WindowState = WindowState.Normal;
        panel.Activate();
    }

    private void HideOnClose(object? sender, CancelEventArgs args)
    {
        if (!exiting) { args.Cancel = true; panel!.Hide(); }
    }

    private async Task ExitAsync()
    {
        if (exiting) return;
        exiting = true;
        lifetime.Cancel();
        updateTimer?.Stop();
        if (tray is not null) tray.Visible = false;
        try
        {
            if (startup is not null) await startup;
            if (updateTask is not null) await updateTask;
            if (backend is not null) await backend.DisposeAsync();
        }
        finally
        {
            browser?.Dispose();
            tray?.Dispose();
            trayImage?.Dispose();
            panel?.Close();
            Shutdown();
        }
    }

    protected override void OnSessionEnding(SessionEndingCancelEventArgs e)
    {
        // stdin EOF requests cleanup; the Job Object is the crash/logoff fallback.
        lifetime.Cancel();
        base.OnSessionEnding(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        tray?.Dispose();
        if (ownsInstance) instance?.ReleaseMutex();
        instance?.Dispose();
        lifetime.Dispose();
        base.OnExit(e);
    }
}

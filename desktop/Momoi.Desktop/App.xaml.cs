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
    private MenuItem? windowUpdateMenu;
    private ReleaseStore? releases;
    private CodeRelease? currentRelease;
    private string? authScript;
    private string? dashboardUrl;
    private Window? panel;
    private Grid panelContent = null!;
    private StartupView? loadingView;
    private bool dashboardReady;
    private Forms.NotifyIcon? tray;
    private Drawing.Icon? trayImage;
    private BackendHost? backend;
    private NapCatHost? napcat;
    private Window? qqPanel;
    private LogWindow? logWindow;
    private WebView2? browser;
    private Task? startup;
    private Task? pipeListener;
    private Task? updateTask;
    private System.Windows.Threading.DispatcherTimer? updateTimer;
    private string workspace = "";

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        if (e.Args.Length == 2 && e.Args[0] == "--update-shell-smoke")
        {
            try { await ComponentUpdateWorker.SmokeAsync(e.Args[1]); Shutdown(0); }
            catch (Exception error) { File.WriteAllText(e.Args[1] + ".smoke-error.log", error.ToString()); Shutdown(1); }
            return;
        }
        if (e.Args.Length == 2 && e.Args[0] == "--install-update")
        {
            await ComponentUpdateWorker.RunAsync(this, e.Args[1]);
            return;
        }
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
        napcat = new NapCatHost(workspace);
        if (Array.IndexOf(e.Args, "--qq-smoke") >= 0)
        {
            try
            {
                string entry = Path.Combine(AppContext.BaseDirectory, "releases", "bundled", "app", "momoi", "desktop", "napcat_entry.cjs");
                await napcat.StartAsync("10000", entry, lifetime.Token);
                File.WriteAllText(Path.Combine(workspace, "qq-smoke.json"), JsonSerializer.Serialize(new { ok = true, status = napcat.Status }));
                await napcat.DisposeAsync();
                Shutdown(0);
            }
            catch (Exception error)
            {
                File.WriteAllText(Path.Combine(workspace, "qq-smoke.json"), JsonSerializer.Serialize(new { ok = false, error = error.ToString() }));
                await napcat.DisposeAsync();
                Shutdown(1);
            }
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
        menu.Items.Add("查看日志", null, (_, _) => Dispatcher.BeginInvoke(ShowLogs));
        menu.Items.Add("QQ 登录", null, (_, _) => Dispatcher.BeginInvoke(async () =>
        {
            try { await OpenQQLoginAsync(); }
            catch (Exception error) { MessageBox.Show(error.Message, "Momoi QQ", MessageBoxButton.OK, MessageBoxImage.Information); }
        }));
        updateMenu = menu.Items.Add("检查更新", null, (_, _) => Dispatcher.BeginInvoke(() => updateTask = CheckAllUpdatesAsync()));
        menu.Items.Add("退出程序", null, (_, _) => Dispatcher.BeginInvoke(() => _ = ExitAsync()));
        tray = new Forms.NotifyIcon { Icon = trayImage, Text = "Momoi", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => Dispatcher.BeginInvoke(ShowPanel);
        panelContent = new Grid();
        var shellLayout = new DockPanel();
        var navigation = CreateWindowMenu();
        DockPanel.SetDock(navigation, Dock.Top);
        shellLayout.Children.Add(navigation);
        shellLayout.Children.Add(panelContent);
        panel = new Window
        {
            Title = "Momoi", Width = Math.Min(1440, SystemParameters.WorkArea.Width * 0.94),
            Height = Math.Min(850, SystemParameters.WorkArea.Height * 0.94), MinWidth = 800, MinHeight = 600,
            WindowStartupLocation = WindowStartupLocation.CenterScreen,
            Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Assets/momoi.png")),
            Content = shellLayout,
        };
        ShowLoading("正在启动…");
        MainWindow = panel;
        panel.Closing += HideOnClose;
        panel.Show();
        pipeListener = ListenForActivationAsync();
        startup = StartServicesAsync();
        await startup;
        if (!exiting && File.Exists(UpdatePlanPath)) updateTask = ResumeUpdatesAsync();
    }

    private void ShowLogs()
    {
        if (logWindow is null)
        {
            logWindow = new LogWindow(workspace);
            logWindow.Closed += (_, _) => logWindow = null;
        }
        logWindow.Show();
        if (logWindow.WindowState == WindowState.Minimized) logWindow.WindowState = WindowState.Normal;
        logWindow.Activate();
    }

    private Menu CreateWindowMenu()
    {
        var menu = new Menu
        {
            Padding = new Thickness(12, 5, 12, 5),
            Background = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(247, 248, 251)),
            Foreground = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(32, 36, 58)),
            FontFamily = new System.Windows.Media.FontFamily("Segoe UI Variable, Segoe UI, DengXian"),
            FontSize = 13,
        };
        MenuItem Item(string label, Action action)
        {
            var item = new MenuItem { Header = label, Padding = new Thickness(14, 7, 14, 7) };
            item.Click += (_, _) => action();
            menu.Items.Add(item);
            return item;
        }
        Item("日志", ShowLogs);
        var qq = new MenuItem { Header = "QQ 设置", Padding = new Thickness(14, 7, 14, 7) };
        var channel = new MenuItem { Header = "消息渠道设置" };
        channel.Click += async (_, _) => await OpenSettingsAsync("channel");
        var login = new MenuItem { Header = "打开 QQ 登录窗口" };
        login.Click += async (_, _) =>
        {
            try { await OpenQQLoginAsync(); }
            catch (Exception error) { MessageBox.Show(error.Message, "Momoi QQ", MessageBoxButton.OK, MessageBoxImage.Information); }
        };
        qq.Items.Add(channel); qq.Items.Add(login); menu.Items.Add(qq);
        Item("配置目录", () =>
        {
            Directory.CreateDirectory(workspace);
            Process.Start(new ProcessStartInfo(workspace) { UseShellExecute = true });
        });
        var audio = new MenuItem { Header = "音频", Padding = new Thickness(14, 7, 14, 7) };
        var devices = new MenuItem { Header = "电话与虚拟设备设置" };
        devices.Click += async (_, _) => await OpenSettingsAsync("channel");
        var voice = new MenuItem { Header = "语音识别与合成设置" };
        voice.Click += async (_, _) => await OpenSettingsAsync("voice");
        audio.Items.Add(devices); audio.Items.Add(voice); menu.Items.Add(audio);
        windowUpdateMenu = Item("检查更新", () => updateTask = CheckAllUpdatesAsync());
        return menu;
    }

    private async Task OpenSettingsAsync(string section)
    {
        ShowPanel();
        if (!dashboardReady || browser?.CoreWebView2 is null || switching)
        {
            MessageBox.Show("面板尚未就绪，请稍后再打开设置。", "Momoi");
            return;
        }
        await browser.CoreWebView2.ExecuteScriptAsync("window.location.hash = " + JsonSerializer.Serialize("settings/" + section));
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
            try
            {
                if (napcat!.ShouldAutoStart(out string botQQ))
                    await napcat.StartAsync(botQQ, QQEntry(), lifetime.Token);
            }
            catch (OperationCanceledException) when (exiting) { }
            catch (Exception error) { if (!exiting) MessageBox.Show($"内置 QQ 未能启动：{error.Message}\n可在消息渠道设置中重试。", "Momoi QQ", MessageBoxButton.OK, MessageBoxImage.Warning); }
            updateTimer = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromSeconds(10) };
            updateTimer.Tick += (_, _) => { updateTimer.Stop(); if (!File.Exists(UpdatePlanPath)) updateTask = CheckAllUpdatesAsync(quiet: true); };
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

    private void ShowLoading(string message)
    {
        if (browser is not null) browser.Visibility = Visibility.Hidden;
        if (loadingView is not null) panelContent.Children.Remove(loadingView);
        loadingView = new StartupView(message);
        panelContent.Children.Add(loadingView);
    }

    private void ShowDashboard()
    {
        if (!dashboardReady || browser is null) return;
        if (loadingView is not null) panelContent.Children.Remove(loadingView);
        loadingView = null;
        browser.Visibility = Visibility.Visible;
    }

    private async Task LoadDashboardAsync(BackendReady ready)
    {
        if (browser is null)
        {
            browser = new WebView2 { Visibility = Visibility.Hidden };
            panelContent.Children.Insert(0, browser);
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: Path.Combine(workspace, "webview"));
            await browser.EnsureCoreWebView2Async(environment);
            browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
            browser.CoreWebView2.Settings.IsWebMessageEnabled = true;
            browser.CoreWebView2.WebMessageReceived += HandleQQMessage;
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
        dashboardReady = false;
        var loaded = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        ulong? navigationId = null;
        void NavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs args)
        {
            if (args.Uri != "about:blank") navigationId = args.NavigationId;
        }
        void NavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs args)
        {
            if (args.NavigationId != navigationId) return;
            if (args.IsSuccess) loaded.TrySetResult(true);
            else loaded.TrySetException(new InvalidOperationException($"面板加载失败：{args.WebErrorStatus}"));
        }
        browser.CoreWebView2.NavigationStarting += NavigationStarting;
        browser.CoreWebView2.NavigationCompleted += NavigationCompleted;
        try
        {
            browser.Source = new Uri(ready.Url);
            await loaded.Task.WaitAsync(TimeSpan.FromSeconds(60), lifetime.Token);
            dashboardReady = true;
            ShowDashboard();
        }
        finally
        {
            browser.CoreWebView2.NavigationStarting -= NavigationStarting;
            browser.CoreWebView2.NavigationCompleted -= NavigationCompleted;
        }
    }

    private async Task UpdateAsync(bool quiet = false, CodeRelease? staged = null)
    {
        if (updateBusy || exiting || releases is null || currentRelease is null || backend is null) return;
        updateBusy = true;
        bool installationRequested = false;
        if (updateMenu is not null) updateMenu.Enabled = false;
        if (windowUpdateMenu is not null) windowUpdateMenu.IsEnabled = false;
        try
        {
            tray!.Text = "Momoi — 正在检查更新";
            LatestRelease? latest = staged is null ? await SignedLatest.FetchAsync(lifetime.Token) : null;
            if ((latest?.ReleaseId ?? staged!.Manifest.ReleaseId) == currentRelease.Manifest.ReleaseId)
            { if (!quiet) MessageBox.Show("当前已是最新发布版本。", "Momoi"); return; }
            if (staged is null && MessageBox.Show($"发现新版本 {latest!.Version}（当前 {currentRelease.Manifest.Version}）。\n是否下载安装？安装完成后会重启后台并刷新面板。", "Momoi 更新", MessageBoxButton.YesNo, MessageBoxImage.Information) != MessageBoxResult.Yes) return;
            installationRequested = true;
            ShowLoading("正在更新…");
            loadingView!.SetDetail("下载中");
            tray!.Text = "Momoi — 正在下载并验证更新";
            var progress = new Progress<UpdateProgress>(value =>
            {
                if (switching || exiting) return;
                string detail = value.Phase;
                if (value.Total > 0)
                {
                    int percent = (int)Math.Clamp(value.Completed * 100 / value.Total, 0, 100);
                    detail += $" · {percent}%";
                    if (value.Phase == "下载中") detail += $" · {value.Completed / 1048576.0:F1} / {value.Total / 1048576.0:F1} MiB";
                }
                loadingView?.SetDetail(detail);
            });
            CodeRelease next = staged ?? await releases.DownloadAsync(latest!, lifetime.Token, progress);
            loadingView?.SetDetail("安装中");
            tray.Text = "Momoi — 正在安装更新";
            switching = true;
            CodeRelease previous = currentRelease;
            string snapshot = Path.Combine(workspace, "update-backups", DateTime.UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N"));
            dashboardReady = false;
            browser?.CoreWebView2.Navigate("about:blank");
            await napcat!.StopAsync();
            qqPanel?.Close();
            await backend.DisposeAsync();
            backend = null;
            try
            {
                await BackendHost.MaintenanceAsync(workspace, previous, snapshot, restore: false);
                releases.Activate(next);
                backend = new BackendHost(workspace);
                loadingView?.SetDetail("启动中");
                var ready = await backend.StartAsync(workspace, next, lifetime.Token);
                currentRelease = next;
                if (napcat!.ShouldAutoStart(out string botQQ))
                    await napcat.StartAsync(botQQ, QQEntry(), lifetime.Token);
                loadingView?.SetDetail("加载面板中");
                await LoadDashboardAsync(ready);
                _ = WatchBackendAsync(backend);
                ShowPanel();
                tray!.ShowBalloonTip(3000, "Momoi 更新完成", $"已安装版本 {next.Manifest.Version}，后台已重启。", Forms.ToolTipIcon.Info);
            }
            catch (Exception updateError)
            {
                loadingView?.SetDetail("恢复原版本中");
                await napcat!.StopAsync();
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
                    if (napcat!.ShouldAutoStart(out string botQQ))
                        await napcat.StartAsync(botQQ, QQEntry(), lifetime.Token);
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
            if (!exiting) ShowDashboard();
            switching = false;
            updateBusy = false;
            if (updateMenu is not null) updateMenu.Enabled = true;
            if (windowUpdateMenu is not null) windowUpdateMenu.IsEnabled = true;
            if (tray is not null && !exiting) tray.Text = "Momoi";
        }
    }

    private static void OpenExternal(string url)
    {
        if (Uri.TryCreate(url, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https")
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }

    private string QQEntry() => Path.Combine(currentRelease!.Directory, "app", "momoi", "desktop", "napcat_entry.cjs");

    private async void HandleQQMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        if (exiting || switching || !Uri.TryCreate(args.Source, UriKind.Absolute, out var source) || source.GetLeftPart(UriPartial.Authority) != dashboardUrl) return;
        string? id = null;
        try
        {
            if (args.WebMessageAsJson.Length > 4096) return;
            using var document = JsonDocument.Parse(args.WebMessageAsJson);
            var root = document.RootElement;
            if (!root.TryGetProperty("type", out var type) || type.GetString() != "momoi-qq") return;
            id = root.GetProperty("id").GetString();
            if (string.IsNullOrEmpty(id) || id.Length > 64) return;
            object result;
            switch (root.GetProperty("action").GetString())
            {
                case "status": result = napcat!.Status; break;
                case "start":
                    result = await napcat!.StartAsync(root.GetProperty("bot_qq").GetString() ?? "", QQEntry(), lifetime.Token);
                    break;
                case "stop": await napcat!.StopAsync(); qqPanel?.Close(); result = napcat.Status; break;
                case "login": await OpenQQLoginAsync(); result = napcat!.Status; break;
                default: throw new ArgumentException("未知 QQ 操作。");
            }
            browser?.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new { type = "momoi-qq", id, result }));
        }
        catch (Exception error)
        {
            if (id is not null && !exiting)
                browser?.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new { type = "momoi-qq", id, error = error.Message }));
        }
    }

    private async Task OpenQQLoginAsync()
    {
        string url = napcat!.LoginUrl;
        if (qqPanel is not null) { qqPanel.Show(); qqPanel.Activate(); return; }
        var view = new WebView2();
        var window = new Window { Title = "Momoi — QQ 登录", Width = 900, Height = 760, Content = view,
            Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Assets/momoi.png")) };
        qqPanel = window;
        window.Closed += (_, _) => { view.Dispose(); if (qqPanel == window) qqPanel = null; };
        window.Show();
        try
        {
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: Path.Combine(workspace, "webview"));
            await view.EnsureCoreWebView2Async(environment);
            view.CoreWebView2.Settings.IsWebMessageEnabled = false;
            view.CoreWebView2.Settings.AreDevToolsEnabled = false;
            string origin = new Uri(url).GetLeftPart(UriPartial.Authority);
            view.CoreWebView2.NavigationStarting += (_, args) =>
            {
                if (!Uri.TryCreate(args.Uri, UriKind.Absolute, out var uri) || uri.GetLeftPart(UriPartial.Authority) != origin)
                { args.Cancel = true; OpenExternal(args.Uri); }
            };
            view.CoreWebView2.NewWindowRequested += (_, args) => { args.Handled = true; OpenExternal(args.Uri); };
            view.Source = new Uri(url);
        }
        catch { window.Close(); throw; }
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
            if (napcat is not null) await napcat.DisposeAsync();
            if (backend is not null) await backend.DisposeAsync();
        }
        finally
        {
            qqPanel?.Close();
            browser?.Dispose();
            logWindow?.Close();
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
        logWindow?.Close();
        tray?.Dispose();
        if (ownsInstance) instance?.ReleaseMutex();
        instance?.Dispose();
        lifetime.Dispose();
        base.OnExit(e);
    }
}

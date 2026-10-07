using System;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;

namespace Momoi.Desktop;

public partial class App
{
    private Grid shellRoot = null!;
    private UpdatePromptView? updatePrompt;
    private Task<bool>? updatePromptTask;

    private async Task<bool> ShowUpdatePromptAsync(string title, string message, string confirm = "知道了", string? cancel = null)
    {
        if (exiting || panel is null) return false;
        while (updatePromptTask is not null)
        {
            ShowPanel();
            await updatePromptTask;
            if (exiting) return false;
        }
        var completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        updatePromptTask = completion.Task;
        var previousFocus = Keyboard.FocusedElement;
        var previousVisibility = browser?.Visibility;
        var content = shellRoot.Children[0] as UIElement;
        bool contentEnabled = content?.IsEnabled ?? true;
        updatePrompt = new UpdatePromptView(title, message, confirm, cancel, result => completion.TrySetResult(result));
        try
        {
            if (browser is not null) browser.Visibility = Visibility.Hidden;
            if (content is not null) content.IsEnabled = false;
            Panel.SetZIndex(updatePrompt, 100);
            shellRoot.Children.Add(updatePrompt);
            ShowPanel();
            using var registration = lifetime.Token.Register(() => completion.TrySetResult(false));
            return await completion.Task;
        }
        finally
        {
            shellRoot.Children.Remove(updatePrompt);
            updatePrompt = null; updatePromptTask = null;
            if (content is not null) content.IsEnabled = contentEnabled;
            if (browser is not null)
            {
                // Startup may have created the WebView while a prompt was open.
                if (!exiting && dashboardReady && loadingView is null) browser.Visibility = Visibility.Visible;
                else if (previousVisibility.HasValue) browser.Visibility = previousVisibility.Value;
            }
            if (!exiting && previousFocus is not null) Keyboard.Focus(previousFocus);
        }
    }
    private async Task RunUpdatePromptSmokeAsync(string directory)
    {
        Directory.CreateDirectory(directory);
        shellRoot = new Grid { Background = (System.Windows.Media.Brush)FindResource("Canvas") };
        var background = new Grid();
        background.Children.Add(new StartupView("正在启动…"));
        shellRoot.Children.Add(background);
        panel = new Window { Width = 980, Height = 650, Content = shellRoot, Title = "Momoi — 更新提示验证" };
        panel.Closing += HideOnClose;
        panel.Show();
        try
        {
            Task<bool> declined = ShowUpdatePromptAsync("发现可用更新", "将按顺序更新：\n外壳 1.1.3\nNapCat / QQ 组件\n主体程序包 1.1.3\n\n安装期间 Momoi 会重启，配置和数据将保留。", "安装并重启", "稍后再说");
            await Task.Delay(150);
            if (background.IsEnabled || shellRoot.Children.Count != 2 || updatePrompt is null) throw new InvalidOperationException("Overlay did not block the application surface");
            ShellPreview.Save(shellRoot, Path.Combine(directory, "update.png"));
            panel.Close();
            if (!panel.IsVisible) throw new InvalidOperationException("Pending prompt was hidden by closing the panel");
            updatePrompt.Cancel!.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            if (await declined || !background.IsEnabled || shellRoot.Children.Count != 1) throw new InvalidOperationException("Declining did not restore the application");
            Task<bool> accepted = ShowUpdatePromptAsync("发现可用更新", "确认验证", "安装并重启", "稍后再说");
            Task<bool> queued = ShowUpdatePromptAsync("更新未完成", "后续提示验证");
            updatePrompt!.Confirm.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            if (!await accepted) throw new InvalidOperationException("Confirm was not accepted");
            await Task.Delay(50);
            if (queued.IsCompleted || updatePrompt is null) throw new InvalidOperationException("A queued prompt inherited another prompt's decision");
            updatePrompt.Confirm.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            if (!await queued || !background.IsEnabled) throw new InvalidOperationException("Queued prompt did not complete independently");
            Task<bool> canceled = ShowUpdatePromptAsync("验证退出", "退出时不得卡住更新任务");
            lifetime.Cancel();
            if (await canceled.WaitAsync(TimeSpan.FromSeconds(2)) || shellRoot.Children.Count != 1) throw new InvalidOperationException("Shutdown did not dismiss the overlay");
            File.WriteAllText(Path.Combine(directory, "prompt-smoke.json"), "{\"ok\":true,\"overlay\":true,\"cancel\":true,\"confirm\":true,\"queued\":true,\"shutdown\":true}");
        }
        finally { exiting = true; panel.Close(); }
    }

}

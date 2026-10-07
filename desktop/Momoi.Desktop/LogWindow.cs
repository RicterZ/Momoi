using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Momoi.Desktop;

internal sealed class LogWindow : Window
{
    private const int MaxDisplayCharacters = 200_000;
    private readonly TextBox output;
    private readonly CheckBox follow;
    private readonly DispatcherTimer timer;
    private long cursor;

    public LogWindow(string workspace)
    {
        Title = "Momoi · 实时日志";
        Width = Math.Min(1200, SystemParameters.WorkArea.Width * 0.9);
        Height = Math.Min(720, SystemParameters.WorkArea.Height * 0.9);
        MinWidth = 650; MinHeight = 400;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Assets/momoi.png"));
        FontFamily = new FontFamily("Segoe UI Variable, Segoe UI, DengXian");
        Background = new SolidColorBrush(Color.FromRgb(247, 248, 251));
        var layout = new DockPanel { Margin = new Thickness(16) };
        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 12) };
        follow = new CheckBox { Content = "自动滚动", IsChecked = true, VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 18, 0) };
        toolbar.Children.Add(follow);
        output = new TextBox
        {
            IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            FontFamily = new FontFamily("Cascadia Mono, Consolas, DengXian"), FontSize = 12,
            Background = new SolidColorBrush(Color.FromRgb(24, 29, 43)), Foreground = new SolidColorBrush(Color.FromRgb(226, 231, 241)),
            Padding = new Thickness(12), BorderThickness = new Thickness(0), IsUndoEnabled = false,
        };
        Button AddButton(string label, RoutedEventHandler handler)
        {
            var button = new Button { Content = label, Padding = new Thickness(14, 6, 14, 6), Margin = new Thickness(0, 0, 8, 0) };
            button.Click += handler; toolbar.Children.Add(button); return button;
        }
        AddButton("复制日志", (_, _) =>
        {
            try { Clipboard.SetText(output.Text); }
            catch (Exception error) { MessageBox.Show(this, "复制失败：" + error.Message, "Momoi"); }
        });
        AddButton("清空窗口", (_, _) => { cursor = LiveLog.ReadAfter(long.MaxValue).Latest; output.Clear(); });
        AddButton("日志目录", (_, _) =>
        {
            try
            {
                string directory = Path.Combine(workspace, "logs");
                Directory.CreateDirectory(directory);
                Process.Start(new ProcessStartInfo(directory) { UseShellExecute = true });
            }
            catch (Exception error) { MessageBox.Show(this, "打开失败：" + error.Message, "Momoi"); }
        });
        DockPanel.SetDock(toolbar, Dock.Top); layout.Children.Add(toolbar);
        var hint = new TextBlock { Text = "实时显示后台、QQ 与语音服务的 stdout / stderr；保留最近输出，完整日志仍写入 data。",
            Foreground = new SolidColorBrush(Color.FromRgb(105, 112, 134)), Margin = new Thickness(0, 0, 0, 10) };
        DockPanel.SetDock(hint, Dock.Top); layout.Children.Add(hint);
        layout.Children.Add(output); Content = layout;
        timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(200) };
        timer.Tick += (_, _) => Refresh();
        Loaded += (_, _) => { Refresh(); timer.Start(); };
        Closed += (_, _) => timer.Stop();
    }

    private void Refresh()
    {
        var batch = LiveLog.ReadAfter(cursor);
        cursor = batch.Latest;
        if (batch.Entries.Length == 0) return;
        double offset = output.VerticalOffset;
        string addition = string.Concat(batch.Entries.Select(item => item.Text));
        if (output.Text.Length + addition.Length > MaxDisplayCharacters)
        {
            string combined = output.Text + addition;
            int start = combined.IndexOf('\n', combined.Length - MaxDisplayCharacters);
            output.Text = start >= 0 ? combined[(start + 1)..] : combined[^MaxDisplayCharacters..];
        }
        else output.AppendText(addition);
        if (follow.IsChecked == true) output.ScrollToEnd();
        else output.ScrollToVerticalOffset(offset);
    }
}

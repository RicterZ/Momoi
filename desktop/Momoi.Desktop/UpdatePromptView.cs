using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Media.Effects;

namespace Momoi.Desktop;

// A native, in-window modal surface. WebView2 must be hidden while it is shown
// because its child HWND would otherwise paint above WPF content.
internal sealed class UpdatePromptView : Grid
{
    internal Button Confirm { get; }
    internal Button? Cancel { get; }

    internal UpdatePromptView(string title, string message, string confirm, string? cancel, Action<bool> complete)
    {
        Background = new SolidColorBrush(Color.FromArgb(110, 32, 36, 58));
        TextElement.SetFontFamily(this, new FontFamily("Segoe UI Variable, Segoe UI, DengXian"));
        TextElement.SetForeground(this, (Brush)Application.Current.FindResource("Ink"));
        Focusable = true;
        KeyboardNavigation.SetTabNavigation(this, KeyboardNavigationMode.Cycle);
        var content = new StackPanel();
        content.Children.Add(new TextBlock { Text = "MOMOI // UPDATE", FontSize = 12, FontWeight = FontWeights.SemiBold, Foreground = (Brush)Application.Current.FindResource("Pink"), Margin = new Thickness(0, 0, 0, 14) });
        content.Children.Add(new TextBlock { Text = title, FontSize = 24, FontWeight = FontWeights.Bold, TextWrapping = TextWrapping.Wrap });
        content.Children.Add(new TextBlock { Text = "GAME DEV DEPT.", FontSize = 11, FontWeight = FontWeights.SemiBold, Foreground = (Brush)Application.Current.FindResource("Muted"), Margin = new Thickness(0, 8, 0, 0) });
        content.Children.Add(new ScrollViewer { MaxHeight = 280, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = new TextBlock { Text = message, FontSize = 14, LineHeight = 24, TextWrapping = TextWrapping.Wrap, Foreground = (Brush)Application.Current.FindResource("Muted") }, Margin = new Thickness(0, 16, 0, 26) });
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        if (cancel is not null)
        {
            Cancel = new Button { Content = cancel, Style = (Style)Application.Current.FindResource("MomoiButton"), Margin = new Thickness(0, 0, 12, 0), MinWidth = 96 };
            Cancel.Click += (_, _) => complete(false);
            buttons.Children.Add(Cancel);
        }
        Confirm = new Button { Content = confirm, Style = (Style)Application.Current.FindResource("MomoiButton"), MinWidth = 110, Background = (Brush)Application.Current.FindResource("Pink"), Foreground = Brushes.White, BorderBrush = (Brush)Application.Current.FindResource("Pink") };
        Confirm.Click += (_, _) => complete(true);
        buttons.Children.Add(Confirm); content.Children.Add(buttons);
        Children.Add(new Border { Child = content, Width = 520, MaxWidth = 600, Margin = new Thickness(24), Padding = new Thickness(32), CornerRadius = new CornerRadius(20), Background = Brushes.White, BorderBrush = (Brush)Application.Current.FindResource("Line"), BorderThickness = new Thickness(1), HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Effect = new DropShadowEffect { Color = Color.FromRgb(32, 36, 58), BlurRadius = 36, ShadowDepth = 8, Opacity = 0.2 } });
        PreviewKeyDown += (_, e) => { if (e.Key == Key.Escape) { e.Handled = true; complete(false); } };
        Loaded += (_, _) => (Cancel ?? Confirm).Focus();
    }
}

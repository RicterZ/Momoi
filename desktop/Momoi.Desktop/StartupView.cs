using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Momoi.Desktop;

// Native counterpart of web/src/Loading.jsx: visible before WebView/backend initialization.
internal sealed class StartupView : Grid
{
    private readonly TextBlock detail;

    public void SetDetail(string text) => detail.Text = text;

    public StartupView(string message = "正在启动…")
    {
        Background = Brush("#f7f8fb");
        var content = new StackPanel
        {
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        var transform = new TransformGroup();
        var rotation = new RotateTransform();
        var translation = new TranslateTransform();
        transform.Children.Add(rotation);
        transform.Children.Add(translation);
        var icon = new Grid
        {
            Width = 39, Height = 39,
            HorizontalAlignment = HorizontalAlignment.Center,
            RenderTransformOrigin = new Point(0.5, 0.5),
            RenderTransform = transform,
        };
        icon.Children.Add(new Border
        {
            Width = 36, Height = 36, CornerRadius = new CornerRadius(10),
            Background = Brush("#43b8f5"),
            HorizontalAlignment = HorizontalAlignment.Left,
            VerticalAlignment = VerticalAlignment.Top,
            RenderTransform = new TranslateTransform(3, 3),
        });
        icon.Children.Add(new Border
        {
            Width = 36, Height = 36, CornerRadius = new CornerRadius(10),
            BorderBrush = Brush("#20243a"), BorderThickness = new Thickness(2),
            Background = Brush("#ff658d"),
            HorizontalAlignment = HorizontalAlignment.Left,
            VerticalAlignment = VerticalAlignment.Top,
            Child = new TextBlock
            {
                Text = "M", Foreground = Brushes.White, FontSize = 12,
                FontFamily = new FontFamily("Segoe UI Variable, Segoe UI"),
                FontWeight = FontWeights.Black,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            },
        });
        content.Children.Add(icon);
        content.Children.Add(new TextBlock
        {
            Text = message, Margin = new Thickness(0, 12, 0, 0),
            Foreground = Brush("#697086"), FontSize = 12,
            FontFamily = new FontFamily("Segoe UI Variable, Segoe UI, DengXian"),
            FontWeight = FontWeights.SemiBold,
            HorizontalAlignment = HorizontalAlignment.Center,
        });
        detail = new TextBlock
        {
            Margin = new Thickness(0, 8, 0, 0), Foreground = Brush("#697086"),
            FontSize = 11, FontFamily = new FontFamily("Segoe UI Variable, Segoe UI, DengXian"),
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        content.Children.Add(detail);
        Children.Add(content);
        Loaded += (_, _) =>
        {
            if (!SystemParameters.ClientAreaAnimation) return;
            translation.BeginAnimation(TranslateTransform.YProperty, Hop(-5));
            rotation.BeginAnimation(RotateTransform.AngleProperty, Hop(3));
        };
        Unloaded += (_, _) =>
        {
            translation.BeginAnimation(TranslateTransform.YProperty, null);
            rotation.BeginAnimation(RotateTransform.AngleProperty, null);
        };
    }

    private static SolidColorBrush Brush(string color) =>
        new((Color)ColorConverter.ConvertFromString(color));

    private static DoubleAnimation Hop(double target) => new(0, target, TimeSpan.FromMilliseconds(900))
    {
        AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever,
        EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
    };
}

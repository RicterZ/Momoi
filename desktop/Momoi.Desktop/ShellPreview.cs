using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace Momoi.Desktop;

internal static class ShellPreview
{
    internal static void Save(FrameworkElement element, string path)
    {
        element.UpdateLayout();
        var image = new RenderTargetBitmap((int)Math.Ceiling(element.ActualWidth), (int)Math.Ceiling(element.ActualHeight), 96, 96, PixelFormats.Pbgra32);
        image.Render(element);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(image));
        using var file = File.Create(path); encoder.Save(file);
    }
    internal static async Task RunAsync(Menu menu, string directory)
    {
        Directory.CreateDirectory(directory);
        var layout = new DockPanel(); DockPanel.SetDock(menu, Dock.Top); layout.Children.Add(menu);
        layout.Children.Add(new StartupView("正在启动…"));
        var window = new Window { Title = "Momoi — 界面预览", Width = 980, Height = 600, Content = layout, WindowStartupLocation = WindowStartupLocation.CenterScreen };
        window.Show(); await Task.Delay(250); Save((FrameworkElement)window.Content, Path.Combine(directory, "menu.png"));
        var socket = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0); socket.Start(); int port = ((IPEndPoint)socket.LocalEndpoint).Port; socket.Stop();
        using var listener = new HttpListener(); listener.Prefixes.Add($"http://127.0.0.1:{port}/"); listener.Start();
        async Task Serve()
        {
            for (int index = 0; index < 2; index++)
            {
                var request = await listener.GetContextAsync();
                string body = request.Request.Url!.AbsolutePath.EndsWith("/devices")
                    ? "{\"inputs\":[{\"id\":\"mic\",\"name\":\"麦克风 (Steam Streaming Microphone)\"}],\"outputs\":[{\"id\":\"speaker\",\"name\":\"扬声器 (Steam Streaming Speakers)\"}]}"
                    : "{\"revision\":\"preview\",\"app\":{}}";
                byte[] bytes = Encoding.UTF8.GetBytes(body); request.Response.ContentType = "application/json";
                await request.Response.OutputStream.WriteAsync(bytes); request.Response.Close();
            }
        }
        Task serving = Serve();
        var devices = new AudioDeviceWindow(new BackendReady($"http://127.0.0.1:{port}", "preview"), new ApplicationAudioRoute(directory)) { Owner = window };
        devices.Show(); await serving.WaitAsync(TimeSpan.FromSeconds(15)); await Task.Delay(500);
        Save((FrameworkElement)devices.Content, Path.Combine(directory, "audio.png"));
        devices.Close(); window.Close();
    }
}

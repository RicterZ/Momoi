using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Momoi.Desktop;

internal sealed record AudioChoice(string Id, string Name)
{
    public override string ToString() => Name;
}

internal sealed class AudioDeviceWindow : Window
{
    private readonly BackendReady connection;
    private readonly ApplicationAudioRoute routes;
    private readonly ComboBox input = new(), output = new();
    private readonly TextBlock status = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 16, 0, 0) };
    private readonly Button apply = new() { Content = "应用", MinWidth = 90 };
    private readonly Button refresh = new() { Content = "刷新设备", Margin = new Thickness(0, 0, 10, 0) };
    private readonly List<AudioChoice> inputs = new(), outputs = new();
    private string revision = "";
    private bool busy;
    public AudioDeviceWindow(BackendReady connection, ApplicationAudioRoute routes)
    {
        this.connection = connection; this.routes = routes;
        Title = "Momoi — 音频设备"; Width = 600; SizeToContent = SizeToContent.Height; ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        FontFamily = new FontFamily("Segoe UI Variable, Segoe UI, DengXian"); FontSize = 13;
        Background = (Brush)Application.Current.FindResource("Canvas"); Foreground = (Brush)Application.Current.FindResource("Ink");
        var layout = new StackPanel { Margin = new Thickness(26) };
        layout.Children.Add(new TextBlock { Text = "电话音频设备", FontSize = 21, FontWeight = FontWeights.Bold });
        layout.Children.Add(new TextBlock { Text = "为内置 QQ 选择输入与输出设备，系统默认设备保持不变。", Foreground = (Brush)Application.Current.FindResource("Muted"), Margin = new Thickness(0, 8, 0, 22), TextWrapping = TextWrapping.Wrap });
        AddChoice(layout, "QQ 输入 · 麦克风", "Momoi 的语音从这里发给 QQ。", input);
        AddChoice(layout, "QQ 输出 · 扬声器", "QQ 对端的语音从这里交给 Momoi 识别。", output);
        status.Foreground = (Brush)Application.Current.FindResource("Muted"); layout.Children.Add(status);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 22, 0, 0) };
        refresh.Style = apply.Style = (Style)Application.Current.FindResource("MomoiButton");
        apply.Background = (Brush)Application.Current.FindResource("Pink"); apply.Foreground = Brushes.White; apply.BorderBrush = apply.Background;
        refresh.Click += async (_, _) => await RefreshAsync(); apply.Click += async (_, _) => await ApplyAsync();
        buttons.Children.Add(refresh); buttons.Children.Add(apply); layout.Children.Add(buttons);
        Content = layout;
        Loaded += async (_, _) => await RefreshAsync();
        Closing += (_, e) => { if (busy) e.Cancel = true; };
    }
    private static void AddChoice(StackPanel layout, string label, string hint, ComboBox choice)
    {
        layout.Children.Add(new TextBlock { Text = label, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 8) });
        choice.Style = (Style)Application.Current.FindResource("MomoiComboBox"); layout.Children.Add(choice);
        layout.Children.Add(new TextBlock { Text = hint, Foreground = (Brush)Application.Current.FindResource("Muted"), Margin = new Thickness(0, 7, 0, 18) });
    }
    private HttpClient Client()
    {
        var client = new HttpClient(new HttpClientHandler { UseProxy = false }) { BaseAddress = new Uri(connection.Url), Timeout = TimeSpan.FromSeconds(20) };
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", connection.Token);
        return client;
    }
    private void SetBusy(bool value)
    {
        busy = value; apply.IsEnabled = refresh.IsEnabled = input.IsEnabled = output.IsEnabled = !value;
    }
    private static string Selected(JsonElement app, string key)
    {
        if (app.TryGetProperty("channels", out var channels) && channels.TryGetProperty("enabled", out var enabled) &&
            enabled.TryGetProperty("napcat", out var napcat) && napcat.TryGetProperty("voice_call", out var voice) && voice.TryGetProperty(key, out var value))
            return value.GetString() ?? "";
        return "";
    }
    private static void Fill(ComboBox box, List<AudioChoice> devices, JsonElement catalog, string group, string selected)
    {
        devices.Clear(); box.Items.Clear();
        box.Items.Add(new AudioChoice("", "自动选择：CABLE → Steam → 其他设备"));
        foreach (var item in catalog.GetProperty(group).EnumerateArray())
        {
            var choice = new AudioChoice(item.GetProperty("id").GetString()!, item.GetProperty("name").GetString()!);
            devices.Add(choice); box.Items.Add(choice);
        }
        if (selected.Length > 0 && devices.All(item => item.Id != selected)) box.Items.Add(new AudioChoice(selected, "已保存的设备不可用，请重新选择"));
        box.SelectedItem = box.Items.Cast<AudioChoice>().First(item => item.Id == selected);
    }
    private async Task RefreshAsync()
    {
        SetBusy(true); status.Text = "正在读取设备…";
        try
        {
            using var client = Client();
            using var settings = JsonDocument.Parse(await client.GetStringAsync("/api/settings/channels/napcat/voice-call/audio-configuration"));
            using var response = await client.GetAsync("/api/settings/channels/napcat/voice-call/devices");
            string body = await response.Content.ReadAsStringAsync();
            using var catalog = JsonDocument.Parse(body);
            if (!response.IsSuccessStatusCode)
            {
                string detail = catalog.RootElement.TryGetProperty("errors", out var errors) && errors.GetArrayLength() > 0
                    ? errors[0].GetString() ?? "暂时无法读取音频设备，请刷新设备列表。"
                    : "暂时无法读取音频设备，请刷新设备列表。";
                throw new InvalidOperationException(detail);
            }
            revision = settings.RootElement.GetProperty("revision").GetString()!;
            var app = settings.RootElement.GetProperty("app");
            Fill(input, inputs, catalog.RootElement, "inputs", Selected(app, "input_device"));
            Fill(output, outputs, catalog.RootElement, "outputs", Selected(app, "output_device"));
            status.Text = "已列出所有可用音频设备。优先 CABLE，其次 Steam 虚拟线路；实体设备可能收音、外放或回声，仍可自行选择。应用后重新拨打电话。";
        }
        catch (Exception error) { status.Text = error.Message; }
        finally { SetBusy(false); }
    }
    private static string DeviceError(string message) =>
        message.Contains("0x80070490", StringComparison.OrdinalIgnoreCase) || message.Contains("0x88890004", StringComparison.OrdinalIgnoreCase)
            ? "所选设备在当前 Windows 会话不可用，请刷新设备或选择其他设备。" : message;

    private async Task ApplyAsync()
    {
        LiveLog.Write("audio-route", "shell", "event=audio_route_apply_started");
        SetBusy(true); apply.Content = "应用中…"; status.Text = "正在保存所选设备…";
        bool savedConfiguration = false;
        try
        {
            var selectedInput = input.SelectedItem as AudioChoice ?? throw new InvalidOperationException("请选择输入设备。");
            var selectedOutput = output.SelectedItem as AudioChoice ?? throw new InvalidOperationException("请选择输出设备。");
            var effectiveInput = selectedInput.Id == "" ? inputs.OrderBy(item => AudioDevicePreference.Rank(item.Name, true)).FirstOrDefault() : inputs.FirstOrDefault(item => item.Id == selectedInput.Id);
            var effectiveOutput = selectedOutput.Id == "" ? outputs.OrderBy(item => AudioDevicePreference.Rank(item.Name, false)).FirstOrDefault() : outputs.FirstOrDefault(item => item.Id == selectedOutput.Id);
            if (effectiveInput is null || effectiveOutput is null) throw new InvalidOperationException("所选音频设备不可用；请手动选择或刷新。");
            LiveLog.Write("audio-route", "shell", $"event=audio_route_selection input={effectiveInput.Name} input_id={effectiveInput.Id} output={effectiveOutput.Name} output_id={effectiveOutput.Id}");
            using var client = Client();
            string payload = JsonSerializer.Serialize(new { input_device = selectedInput.Id, output_device = selectedOutput.Id, revision });
            using var response = await client.PutAsync("/api/settings/channels/napcat/voice-call/devices", new StringContent(payload, Encoding.UTF8, "application/json"));
            string body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException(response.StatusCode == System.Net.HttpStatusCode.Conflict ? "配置已被修改，请刷新后重新应用。" : response.StatusCode == System.Net.HttpStatusCode.MethodNotAllowed ? "主体程序尚未更新，请先点击检查更新完成升级。" : body);
            savedConfiguration = true;
            LiveLog.Write("audio-route", "shell", "event=audio_route_configuration_saved");
            using (var saved = JsonDocument.Parse(body)) revision = saved.RootElement.GetProperty("revision").GetString()!;
            var processes = new List<object>();
            foreach (var process in Process.GetProcessesByName("QQ"))
            {
                using (process)
                {
                    try { processes.Add(new { pid = process.Id }); }
                    catch (InvalidOperationException) { }
                }
            }
            using var host = JsonDocument.Parse(JsonSerializer.Serialize(new { audioProcesses = processes }));
            using var devices = JsonDocument.Parse(JsonSerializer.Serialize(new { audio_devices = new { input_device = new { id = effectiveInput.Id }, output_device = new { id = effectiveOutput.Id } } }));
            status.Text = "选择已保存，正在应用 QQ 音频路由…";
            await Dispatcher.InvokeAsync(() => { }, System.Windows.Threading.DispatcherPriority.Render);
            bool routed = routes.Ensure(host.RootElement, devices.RootElement);
            LiveLog.Write("audio-route", "shell", $"event=audio_route_apply_completed routed={routed}");
            if (!routed)
            {
                status.Text = $"{DateTime.Now:HH:mm:ss} · 选择已保存，QQ 音频进程尚未就绪，请登录内置 QQ 后重试。";
                return;
            }
            status.Text = "路由已应用，正在检查语音设备连接…";
            using var probe = await client.PostAsync("/api/settings/channels/napcat/voice-call/test", new StringContent("{}", Encoding.UTF8, "application/json"));
            probe.EnsureSuccessStatusCode();
            using var result = JsonDocument.Parse(await probe.Content.ReadAsStringAsync());
            bool ready = result.RootElement.GetProperty("ok").GetBoolean();
            string reason = result.RootElement.TryGetProperty("error", out var failure) ? failure.GetString() ?? "语音服务尚未就绪" : "语音服务尚未就绪";
            LiveLog.Write("audio-route", ready ? "shell" : "stderr", $"event=audio_route_connection_checked ready={ready} error={(ready ? "" : reason)}");
            status.Text = $"{DateTime.Now:HH:mm:ss} · " + (ready ? "设备已保存并应用，语音连接已就绪。请重新拨打电话。" : "选择已保存，设备尚未连通：" + DeviceError(reason));
        }
        catch (Exception error) { status.Text = $"{DateTime.Now:HH:mm:ss} · " + (savedConfiguration ? "选择已保存，连接失败：" : "应用失败：") + DeviceError(error.Message); LiveLog.Write("audio-route", "stderr", error.ToString()); }
        finally { apply.Content = "应用"; SetBusy(false); }
    }
}

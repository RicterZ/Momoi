using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows.Threading;

namespace Momoi.Desktop;

public partial class App
{
    private DispatcherTimer? audioRouteTimer;
    private bool applyingAudioRoute;
    private string lastAudioRouteError = "";

    private void RestoreAudioRouting()
    {
        try { audioRoutes?.Close(); }
        catch (Exception error) { LiveLog.Write("audio-route", "stderr", "恢复 QQ 音频路由失败，保留备份供下次启动恢复：" + error); }
    }

    private void StartAudioRouting()
    {
        audioRouteTimer ??= new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        audioRouteTimer.Tick -= AudioRouteTick;
        audioRouteTimer.Tick += AudioRouteTick;
        audioRouteTimer.Start();
        AudioRouteTick(null, EventArgs.Empty);
    }

    private async void AudioRouteTick(object? sender, EventArgs e)
    {
        if (applyingAudioRoute || exiting || switching || updateBusy || !dashboardReady || panelConnection is null) return;
        applyingAudioRoute = true;
        try
        {
            using var client = new HttpClient(new HttpClientHandler { UseProxy = false }) { BaseAddress = new Uri(panelConnection.Url), Timeout = TimeSpan.FromSeconds(5) };
            client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", panelConnection.Token);
            using var settings = JsonDocument.Parse(await client.GetStringAsync("/api/settings/channels/napcat/voice-call/audio-configuration", lifetime.Token));
            var app = settings.RootElement.GetProperty("app");
            if (!app.TryGetProperty("channels", out var channels) || !channels.TryGetProperty("enabled", out var enabled) ||
                !enabled.TryGetProperty("napcat", out var napcatOptions) || !napcatOptions.TryGetProperty("voice_call", out var voice) ||
                !voice.TryGetProperty("enabled", out var callEnabled) || callEnabled.ValueKind != JsonValueKind.True) { RestoreAudioRouting(); return; }
            using var response = await client.GetAsync("/api/settings/channels/napcat/voice-call/devices", lifetime.Token);
            if (!response.IsSuccessStatusCode) return;
            using var catalog = JsonDocument.Parse(await response.Content.ReadAsStringAsync(lifetime.Token));
            string Select(string role, string group, bool input)
            {
                string selected = voice.TryGetProperty(role, out var value) ? value.GetString() ?? "" : "";
                var matches = catalog.RootElement.GetProperty(group).EnumerateArray()
                    .Where(item => selected.Length == 0 || item.GetProperty("id").GetString() == selected)
                    .OrderBy(item => AudioDevicePreference.Rank(item.GetProperty("name").GetString() ?? "", input)).ToArray();
                return matches.Length > 0 ? matches[0].GetProperty("id").GetString()! : "";
            }
            string input = Select("input_device", "inputs", true);
            string output = Select("output_device", "outputs", false);
            if (input.Length == 0 || output.Length == 0) return;
            var processes = new List<int>();
            foreach (var process in Process.GetProcessesByName("QQ")) using (process)
                try { processes.Add(process.Id); } catch (InvalidOperationException) { }
            if (exiting || switching || updateBusy || napcat?.Running != true) return;
            audioRoutes ??= new ApplicationAudioRoute(System.IO.Path.Combine(workspace, "qq-call"));
            audioRoutes.Ensure(processes, input, output);
            lastAudioRouteError = "";
        }
        catch (OperationCanceledException) when (exiting) { }
        catch (Exception error)
        {
            if (lastAudioRouteError != error.Message) LiveLog.Write("audio-route", "stderr", error.ToString());
            lastAudioRouteError = error.Message;
        }
        finally { applyingAudioRoute = false; }
    }
}

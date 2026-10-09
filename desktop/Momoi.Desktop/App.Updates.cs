using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Threading;
using System.Windows;
using Momoi.Update;

namespace Momoi.Desktop;

internal sealed record PendingUpdates(string Catalog, string? ShellArchive, string? NapCatArchive, string? CodeReleaseId, string? ASRArchive = null);

public partial class App
{
    private string UpdatePlanPath => Path.Combine(workspace, "updates", "pending.json");

    private static string ShellIdentity()
    {
        var assembly = Assembly.GetExecutingAssembly();
        string version = assembly.GetName().Version?.ToString(3) ?? "0.0.0";
        string info = assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "";
        var match = Regex.Match(info, @"\+([0-9a-f]{7,40})");
        return match.Success ? version + "-" + match.Groups[1].Value[..7] : version;
    }

    private IProgress<UpdateProgress> UpdateProgressView() => new Progress<UpdateProgress>(value =>
    {
        string detail = value.Phase;
        if (value.Total > 0) detail += $" · {Math.Clamp(value.Completed * 100 / value.Total, 0, 100)}%";
        loadingView?.SetDetail(detail);
    });

    private async Task<bool> LocalASRSelectedAsync(CancellationToken token)
    {
        if (panelConnection is null) return false;
        using var client = new HttpClient(new HttpClientHandler { UseProxy = false }) { BaseAddress = new Uri(panelConnection.Url), Timeout = TimeSpan.FromSeconds(5) };
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", panelConnection.Token);
        using var snapshot = JsonDocument.Parse(await client.GetStringAsync("/api/settings/configuration", token));
        if (!snapshot.RootElement.TryGetProperty("capabilities", out var capabilities) || !capabilities.TryGetProperty("asr", out var asr)) return false;
        if (!asr.TryGetProperty("adapter", out var adapter) || adapter.GetString() != "sherpa" ||
            (asr.TryGetProperty("enabled", out var enabled) && enabled.ValueKind == JsonValueKind.False)) return false;
        if (!asr.TryGetProperty("options", out var options)) return true;
        foreach (string key in new[] { "endpoint", "model_path" })
            if (options.TryGetProperty(key, out var value) && !string.IsNullOrWhiteSpace(value.GetString())) return false;
        return true;
    }

    private async Task CheckAllUpdatesAsync(bool quiet = false)
    {
        if (updateBusy)
        {
            if (!quiet)
            {
                if (updatePrompt is not null) ShowPanel();
                else await ShowUpdatePromptAsync("正在处理更新", "正在检查或处理更新，请稍候。");
            }
            return;
        }
        if (exiting) return;
        if (releases is null || currentRelease is null)
        {
            if (!quiet) await ShowUpdatePromptAsync("程序正在启动", "启动完成后即可检查更新。");
            return;
        }
        updateBusy = true;
        try
        {
            SetUpdateMenus(false);
            LiveLog.Write("update", "stdout", "Checking signed update catalog…");
            using var checkDeadline = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            checkDeadline.CancelAfter(TimeSpan.FromSeconds(20));
            byte[] envelope = await UpdateCatalog.FetchEnvelopeAsync(checkDeadline.Token);
            var catalog = UpdateCatalog.Verify(envelope, SignedLatest.EmbeddedPublicKey());
            string shellVersion = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "0.0.0";
            bool shell = Version.Parse(catalog.Shell.Version) > Version.Parse(shellVersion) ||
                (Version.Parse(catalog.Shell.Version) == Version.Parse(shellVersion) && ShellIdentity() != catalog.Shell.Id);
            string pairPath = Path.Combine(AppContext.BaseDirectory, "runtime", "qq-pair", "pair-id.txt");
            string marker = Path.Combine(workspace, "updates", "napcat-installed-id.txt");
            string pair = File.Exists(marker) ? File.ReadAllText(marker).Trim() : File.Exists(pairPath) ? File.ReadAllText(pairPath).Trim() : "";
            bool napcatUpdate = pair != catalog.NapCat.Id;
            string asrMarker = Path.Combine(AppContext.BaseDirectory, "runtime", "asr", "component-id.txt");
            bool asrWanted = File.Exists(asrMarker) || await LocalASRSelectedAsync(checkDeadline.Token);
            bool asrUpdate = catalog.ASR is not null && asrWanted && (!File.Exists(asrMarker) || File.ReadAllText(asrMarker).Trim() != catalog.ASR.Id);
            LatestRelease? code = await SignedLatest.FetchAsync(checkDeadline.Token);
            if (code is not null && code.Version != catalog.Version) throw new InvalidDataException("发布正在切换，请稍后重新检查更新。");
            bool codeUpdate = code is not null && code.ReleaseId != currentRelease.Manifest.ReleaseId;
            if (!shell && !napcatUpdate && !codeUpdate && !asrUpdate) { if (!quiet) await ShowUpdatePromptAsync("已是最新版本", "外壳、QQ 组件和主体程序均已是最新版本。"); return; }
            var names = new List<string>();
            if (shell) names.Add("外壳 " + catalog.Shell.Version);
            if (napcatUpdate) names.Add("NapCat / QQ 组件 " + catalog.NapCat.Version);
            if (asrUpdate) names.Add("本地 ASR 组件 " + catalog.ASR!.Version);
            if (codeUpdate) names.Add("主体程序包 " + code!.Version);
            LiveLog.Write("update", "stdout", "Updates available: " + string.Join(", ", names));
            if (!await ShowUpdatePromptAsync("发现可用更新", "将按顺序更新：\n" + string.Join("\n", names) + "\n\n安装期间 Momoi 会重启，配置和数据将保留。", "安装并重启", "稍后再说")) return;
            ShowLoading("正在更新…");
            string directory = Path.Combine(workspace, "updates", Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            string catalogPath = Path.Combine(directory, "catalog.json");
            File.WriteAllBytes(catalogPath, envelope);
            // Stage and verify every download before replacing anything.
            string? shellArchive = shell ? await UpdateCatalog.DownloadAsync(catalog.Shell, directory, UpdateProgressView(), lifetime.Token) : null;
            string? napcatArchive = napcatUpdate ? await UpdateCatalog.DownloadAsync(catalog.NapCat, directory, UpdateProgressView(), lifetime.Token) : null;
            string? asrArchive = asrUpdate ? await UpdateCatalog.DownloadAsync(catalog.ASR!, directory, UpdateProgressView(), lifetime.Token) : null;
            CodeRelease? staged = codeUpdate ? await releases.DownloadAsync(code!, lifetime.Token, UpdateProgressView()) : null;
            var plan = new PendingUpdates(catalogPath, shellArchive, napcatArchive, staged?.Manifest.ReleaseId, asrArchive);
            File.WriteAllText(UpdatePlanPath + ".tmp", JsonSerializer.Serialize(plan));
            File.Move(UpdatePlanPath + ".tmp", UpdatePlanPath, true);
        }
        catch (Exception error)
        {
            LiveLog.Write("update", "stderr", error.ToString());
            if (!exiting && !quiet) await ShowUpdatePromptAsync("更新未完成", error is OperationCanceledException ? "请求超时，请检查网络后重试。" : error.Message);
            return;
        }
        finally
        {
            updateBusy = false;
            SetUpdateMenus(true);
            if (!exiting) ShowDashboard();
        }
        if (File.Exists(UpdatePlanPath)) await ResumeUpdatesAsync();
    }

    private void SetUpdateMenus(bool enabled)
    {
        // Keep the command reachable while a background check is running.
        string label = enabled ? "检查更新" : "正在检查 / 更新…";
        if (updateMenu is not null) { updateMenu.Enabled = true; updateMenu.Text = label; }
        if (windowUpdateMenu is not null) { windowUpdateMenu.IsEnabled = true; windowUpdateMenu.Header = label; }
    }

    private async Task ResumeUpdatesAsync()
    {
        if (exiting || updateBusy || releases is null || currentRelease is null) return;
        try
        {
            var plan = JsonSerializer.Deserialize<PendingUpdates>(File.ReadAllText(UpdatePlanPath)) ?? throw new InvalidDataException("更新计划为空。");
            var catalog = UpdateCatalog.Verify(File.ReadAllBytes(plan.Catalog), SignedLatest.EmbeddedPublicKey());
            string shellVersion = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "0.0.0";
            bool shellPending = plan.ShellArchive is not null && (Version.Parse(shellVersion) < Version.Parse(catalog.Shell.Version) ||
                (Version.Parse(shellVersion) == Version.Parse(catalog.Shell.Version) && ShellIdentity() != catalog.Shell.Id));
            string marker = Path.Combine(workspace, "updates", "napcat-installed-id.txt");
            bool napcatPending = plan.NapCatArchive is not null && (!File.Exists(marker) || File.ReadAllText(marker).Trim() != catalog.NapCat.Id);
            string asrMarker = Path.Combine(AppContext.BaseDirectory, "runtime", "asr", "component-id.txt");
            bool asrPending = plan.ASRArchive is not null && catalog.ASR is not null && (!File.Exists(asrMarker) || File.ReadAllText(asrMarker).Trim() != catalog.ASR.Id);
            if (shellPending || napcatPending || asrPending)
            {
                ShowLoading("正在更新…");
                loadingView?.SetDetail(shellPending ? "安装外壳中" : napcatPending ? "安装 NapCat / QQ 组件中" : "安装本地 ASR 组件中");
                ComponentUpdateWorker.Launch(AppContext.BaseDirectory, shellPending ? plan.ShellArchive! : napcatPending ? plan.NapCatArchive! : plan.ASRArchive!, shellPending ? "shell-zip" : napcatPending ? "napcat-installer" : "asr-installer", plan.Catalog);
                // Do not await ExitAsync here: it normally waits for updateTask itself.
                updateTask = null;
                _ = ExitAsync();
                return;
            }
            if (plan.CodeReleaseId is not null && currentRelease.Manifest.ReleaseId != plan.CodeReleaseId)
            {
                await UpdateAsync(staged: releases.GetRelease(plan.CodeReleaseId));
                if (currentRelease.Manifest.ReleaseId != plan.CodeReleaseId) throw new IOException("主体程序包更新失败，已停止后续步骤。");
            }
            File.Delete(UpdatePlanPath);
        }
        catch (Exception error)
        {
            // A failed step must not loop automatically at every restart.
            if (File.Exists(UpdatePlanPath)) File.Move(UpdatePlanPath, UpdatePlanPath + ".failed", true);
            await ShowUpdatePromptAsync("更新已停止", error.Message + "\n可通过检查更新重新尝试。");
        }
    }
}

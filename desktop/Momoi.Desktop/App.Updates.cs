using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using Momoi.Update;

namespace Momoi.Desktop;

internal sealed record PendingUpdates(string Catalog, string? ShellArchive, string? NapCatArchive, string? CodeReleaseId);

public partial class App
{
    private string UpdatePlanPath => Path.Combine(workspace, "updates", "pending.json");

    private IProgress<UpdateProgress> UpdateProgressView() => new Progress<UpdateProgress>(value =>
    {
        string detail = value.Phase;
        if (value.Total > 0) detail += $" · {Math.Clamp(value.Completed * 100 / value.Total, 0, 100)}%";
        loadingView?.SetDetail(detail);
    });

    private async Task CheckAllUpdatesAsync(bool quiet = false)
    {
        if (updateBusy || exiting || releases is null || currentRelease is null) return;
        updateBusy = true;
        try
        {
            SetUpdateMenus(false);
            byte[] envelope = await UpdateCatalog.FetchEnvelopeAsync(lifetime.Token);
            var catalog = UpdateCatalog.Verify(envelope, SignedLatest.EmbeddedPublicKey());
            string shellVersion = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "0.0.0";
            bool shell = Version.Parse(catalog.Shell.Version) > Version.Parse(shellVersion);
            string pairPath = Path.Combine(AppContext.BaseDirectory, "runtime", "qq-pair", "pair-id.txt");
            string marker = Path.Combine(workspace, "updates", "napcat-installed-id.txt");
            string pair = File.Exists(marker) ? File.ReadAllText(marker).Trim() : File.Exists(pairPath) ? File.ReadAllText(pairPath).Trim() : "";
            bool napcatUpdate = pair != catalog.NapCat.Id;
            LatestRelease? code = await SignedLatest.FetchAsync(lifetime.Token);
            if (code is not null && code.Version != catalog.Version) throw new InvalidDataException("发布正在切换，请稍后重新检查更新。");
            bool codeUpdate = code is not null && code.ReleaseId != currentRelease.Manifest.ReleaseId;
            if (!shell && !napcatUpdate && !codeUpdate) { if (!quiet) MessageBox.Show("当前已是最新版本。", "Momoi 更新"); return; }
            var names = new List<string>();
            if (shell) names.Add("外壳 " + catalog.Shell.Version);
            if (napcatUpdate) names.Add("NapCat / QQ 组件 " + catalog.NapCat.Version);
            if (codeUpdate) names.Add("主体程序包 " + code!.Version);
            if (MessageBox.Show("将按以下顺序更新：\n" + string.Join("\n", names) + "\n\n更新时 Momoi 会重启；配置与数据保留。是否安装？", "Momoi 更新", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
            ShowLoading("正在更新…");
            string directory = Path.Combine(workspace, "updates", Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            string catalogPath = Path.Combine(directory, "catalog.json");
            File.WriteAllBytes(catalogPath, envelope);
            // Stage and verify every download before replacing anything.
            string? shellArchive = shell ? await UpdateCatalog.DownloadAsync(catalog.Shell, directory, UpdateProgressView(), lifetime.Token) : null;
            string? napcatArchive = napcatUpdate ? await UpdateCatalog.DownloadAsync(catalog.NapCat, directory, UpdateProgressView(), lifetime.Token) : null;
            CodeRelease? staged = codeUpdate ? await releases.DownloadAsync(code!, lifetime.Token, UpdateProgressView()) : null;
            var plan = new PendingUpdates(catalogPath, shellArchive, napcatArchive, staged?.Manifest.ReleaseId);
            File.WriteAllText(UpdatePlanPath + ".tmp", JsonSerializer.Serialize(plan));
            File.Move(UpdatePlanPath + ".tmp", UpdatePlanPath, true);
        }
        catch (Exception error)
        {
            if (!exiting && !quiet) MessageBox.Show("检查或下载更新失败：" + error.Message, "Momoi 更新", MessageBoxButton.OK, MessageBoxImage.Error);
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
        if (updateMenu is not null) updateMenu.Enabled = enabled;
        if (windowUpdateMenu is not null) windowUpdateMenu.IsEnabled = enabled;
    }

    private async Task ResumeUpdatesAsync()
    {
        if (exiting || updateBusy || releases is null || currentRelease is null) return;
        try
        {
            var plan = JsonSerializer.Deserialize<PendingUpdates>(File.ReadAllText(UpdatePlanPath)) ?? throw new InvalidDataException("更新计划为空。");
            var catalog = UpdateCatalog.Verify(File.ReadAllBytes(plan.Catalog), SignedLatest.EmbeddedPublicKey());
            string shellVersion = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "0.0.0";
            bool shellPending = plan.ShellArchive is not null && Version.Parse(shellVersion) < Version.Parse(catalog.Shell.Version);
            string marker = Path.Combine(workspace, "updates", "napcat-installed-id.txt");
            bool napcatPending = plan.NapCatArchive is not null && (!File.Exists(marker) || File.ReadAllText(marker).Trim() != catalog.NapCat.Id);
            if (shellPending || napcatPending)
            {
                ShowLoading("正在更新…");
                loadingView?.SetDetail(shellPending ? "安装外壳中" : "安装 NapCat / QQ 组件中");
                ComponentUpdateWorker.Launch(AppContext.BaseDirectory, shellPending ? plan.ShellArchive! : plan.NapCatArchive!, shellPending ? "shell-zip" : "napcat-installer", plan.Catalog);
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
            MessageBox.Show("更新已停止：" + error.Message + "\n可通过检查更新重新尝试。", "Momoi 更新", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }
}

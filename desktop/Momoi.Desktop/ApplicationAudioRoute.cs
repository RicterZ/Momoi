using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;
using AudioRoutePoc;

namespace Momoi.Desktop;

internal sealed class ApplicationAudioRoute
{
    internal sealed record Preference(int Flow, int Role, string Previous, string Desired);
    internal sealed record Backup(string Executable, List<Preference> Preferences);
    private readonly string executable, backupPath, readyPath;
    private int routedPid;
    private long routedStart;
    private string input = "", output = "";
    public ApplicationAudioRoute(string directory)
    {
        executable = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "runtime", "qq-call", "qq", "Files", "QQ.exe"));
        backupPath = Path.Combine(directory, "app-audio-backup.json");
        readyPath = Path.Combine(directory, "app-audio-ready.json");
        File.Delete(readyPath);
    }
    private Process Inspect(int pid)
    {
        var process = Process.GetProcessById(pid);
        if (!string.Equals(process.MainModule?.FileName, executable, StringComparison.OrdinalIgnoreCase)
            || process.SessionId != Process.GetCurrentProcess().SessionId)
        {
            process.Dispose();
            throw new IOException("拒绝修改非私有 QQ 音频进程。");
        }
        return process;
    }
    private static void AtomicWrite<T>(string path, T value)
    {
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(value));
        File.Move(temporary, path, true);
    }
    private Backup? ReadBackup()
    {
        if (!File.Exists(backupPath)) return null;
        var saved = JsonSerializer.Deserialize<Backup>(File.ReadAllText(backupPath));
        if (saved is null || !string.Equals(saved.Executable, executable, StringComparison.OrdinalIgnoreCase))
            throw new IOException("音频路由备份不属于当前私有 QQ。");
        return saved;
    }
    internal static bool ShouldRestore(string current, Preference preference) => current == preference.Desired;
    private void Restore(Policy policy, int pid)
    {
        var saved = ReadBackup();
        if (saved is null) return;
        foreach (var item in saved.Preferences)
        {
            if (!ShouldRestore(policy.Get(pid, item.Flow, item.Role), item)) continue;
            policy.Set(pid, item.Flow, item.Role, item.Previous);
            if (policy.Get(pid, item.Flow, item.Role) != item.Previous) throw new IOException("原应用音频路由恢复回读失败。");
        }
        File.Delete(backupPath);
        LiveLog.Write("audio-route", "shell", $"已恢复私有 QQ 原音频路由 PID={pid}");
    }
    public bool Ensure(JsonElement host, JsonElement media)
    {
        if (!host.TryGetProperty("audioProcesses", out var processes)
            || !media.TryGetProperty("audio_devices", out var devices)
            || !devices.TryGetProperty("input_device", out var inputDevice)
            || !devices.TryGetProperty("output_device", out var outputDevice))
        { File.Delete(readyPath); return false; }
        string desiredInput = inputDevice.GetProperty("id").GetString() ?? "";
        string desiredOutput = outputDevice.GetProperty("id").GetString() ?? "";
        if (!desiredInput.StartsWith("{0.0.1.00000000}.") || !desiredOutput.StartsWith("{0.0.0.00000000}."))
            throw new IOException("虚拟音频端点方向无效。");
        var pids = processes.EnumerateArray().Select(item => item.GetProperty("pid").GetInt32()).ToArray();
        if (pids.Contains(routedPid) && input == desiredInput && output == desiredOutput)
        {
            try
            {
                using var existing = Inspect(routedPid);
                if (existing.StartTime.ToUniversalTime().Ticks == routedStart)
                {
                    using var check = new Policy();
                    bool matches = new[] { 0, 1 }.All(flow => new[] { 0, 1, 2 }.All(role => check.Get(routedPid, flow, role) == Policy.Pack(flow == 0 ? output : input, flow)));
                    if (matches) return true;
                }
            }
            catch (Exception error) when (error is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { }
        }
        File.Delete(readyPath);
        // Only an audio-owning process accepts this API; other Electron children return E_INVALIDARG.
        using var policy = new Policy();
        foreach (int pid in pids)
        {
            Process process;
            try { process = Inspect(pid); }
            catch (Exception error) when (error is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception or IOException) { continue; }
            using (process)
            {
                var original = new List<Preference>();
                try
                {
                    foreach (int flow in new[] { 0, 1 }) foreach (int role in new[] { 0, 1, 2 })
                        original.Add(new Preference(flow, role, policy.Get(pid, flow, role), Policy.Pack(flow == 0 ? desiredOutput : desiredInput, flow)));
                }
                catch (COMException error) when (error.HResult == unchecked((int)0x80070057)) { continue; }
                // Recover a previous shell crash or configuration switch before making a new snapshot.
                if (ReadBackup() is not null)
                {
                    Restore(policy, pid);
                    original = original.Select(item => item with { Previous = policy.Get(pid, item.Flow, item.Role) }).ToList();
                }
                AtomicWrite(backupPath, new Backup(executable, original));
                try
                {
                    foreach (var item in original)
                    {
                        policy.Set(pid, item.Flow, item.Role, item.Desired);
                        if (policy.Get(pid, item.Flow, item.Role) != item.Desired) throw new IOException("应用音频路由回读不匹配。");
                    }
                }
                catch { Restore(policy, pid); throw; }
                routedPid = pid; routedStart = process.StartTime.ToUniversalTime().Ticks;
                input = desiredInput; output = desiredOutput;
                AtomicWrite(readyPath, new { input_device = input, output_device = output, pid });
                LiveLog.Write("audio-route", "shell", $"私有 QQ 应用路由已验证 PID={pid} input={input} output={output}");
                return true;
            }
        }
        return false;
    }
    public void Close()
    {
        File.Delete(readyPath);
        if (!File.Exists(backupPath)) return;
        using var policy = new Policy();
        // App preferences survive PID changes. Restore through a live process at the same exact private path.
        foreach (var process in Process.GetProcessesByName("QQ"))
        {
            using (process)
            {
                try { using var verified = Inspect(process.Id); Restore(policy, process.Id); routedPid = 0; return; }
                catch (COMException error) when (error.HResult == unchecked((int)0x80070057)) { }
                catch (Exception error) when (error is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { }
                catch (IOException error) when (error.Message == "拒绝修改非私有 QQ 音频进程。") { }
            }
        }
        LiveLog.Write("audio-route", "shell", "音频进程已退出，保留备份供下次启动恢复。");
    }
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Windows.Forms;
using Microsoft.Win32;

namespace AudioRoutePoc;
internal sealed record Target(int Pid, string Path, long Started)
{
    public override string ToString() => $"PID {Pid} · {Path}";
    public void Verify()
    {
        using var p = Process.GetProcessById(Pid);
        if (!string.Equals(p.MainModule?.FileName, Path, StringComparison.OrdinalIgnoreCase) || p.StartTime.ToUniversalTime().Ticks != Started)
            throw new InvalidOperationException("目标进程已经变化，请重新选择。");
    }
}
internal sealed record Endpoint(string Id, string Name) { public override string ToString() => Name; }
internal sealed record Saved(Target Target, int Flow, int Role, string Previous, string Desired);
internal sealed class MainForm : Form
{
    private readonly ComboBox targets = new() { Width = 1050, DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly ComboBox input = new() { Width = 1050, DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly ComboBox output = new() { Width = 1050, DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly TextBox log = new() { Multiline = true, ReadOnly = true, WordWrap = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
    private readonly string recovery = Path.Combine(AppContext.BaseDirectory, "routing-backup.json");
    private readonly List<Saved> saved = new();
    public MainForm()
    {
        Text = "AudioRoute PoC · 仅修改选定应用的音频设备"; Width = 1150; Height = 750;
        Font = new System.Drawing.Font("Segoe UI", 10);
        var top = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new Padding(12) };
        top.Controls.Add(new Label { Text = "目标 QQ 进程（私有宿主及 Pepper Plugin 进程可能需分别测试）", AutoSize = true }); top.Controls.Add(targets);
        top.Controls.Add(new Label { Text = "QQ 输入／麦克风", AutoSize = true }); top.Controls.Add(input);
        top.Controls.Add(new Label { Text = "QQ 输出／扬声器", AutoSize = true }); top.Controls.Add(output);
        var buttons = new FlowLayoutPanel { AutoSize = true };
        void Button(string label, Action action) { var b = new Button { Text = label, AutoSize = true, Padding = new Padding(8) }; b.Click += (_, _) => Run(action); buttons.Controls.Add(b); }
        Button("刷新设备和进程", RefreshChoices); Button("读取应用路由", Probe); Button("应用并验证", Apply); Button("恢复原设置", Restore);
        top.Controls.Add(buttons);
        top.Controls.Add(new Label { Text = "不修改系统默认设备。先读取成功再应用；首次应用会保存原设置。关闭时恢复，异常退出后可重新打开恢复。", AutoSize = true });
        Controls.Add(log); Controls.Add(top);
        if (File.Exists(recovery)) saved.AddRange(JsonSerializer.Deserialize<List<Saved>>(File.ReadAllText(recovery)) ?? new());
        Shown += (_, _) => Run(RefreshChoices);
        FormClosing += (_, e) => { if (saved.Count > 0) { try { Restore(); } catch (Exception error) { e.Cancel = true; Write("恢复失败：" + error.Message + "；备份保留，可重试恢复。"); } } };
    }
    private void Run(Action action) { try { action(); } catch (Exception e) { Write($"ERROR 0x{e.HResult:X8}: {e.Message}"); } }
    private void Write(string value) { log.AppendText($"{DateTime.Now:HH:mm:ss.fff} {value}{Environment.NewLine}"); File.AppendAllText(Path.Combine(AppContext.BaseDirectory, "poc.log"), value + Environment.NewLine); }
    internal static List<Endpoint> Devices(int flow)
    {
        var result = new List<Endpoint>();
        using var root = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\" + (flow == 0 ? "Render" : "Capture"));
        if (root is null) return result;
        foreach (string key in root.GetSubKeyNames())
        {
            using var device = root.OpenSubKey(key); if (device?.GetValue("DeviceState") is not int state || (state & 15) != 1) continue;
            using var properties = device.OpenSubKey("Properties");
            string? label = properties?.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},2") as string;
            string? adapter = properties?.GetValue("{b3f8fa53-0004-438e-9003-51a46e139bfc},6") as string;
            string name = !string.IsNullOrWhiteSpace(adapter) ? (string.IsNullOrWhiteSpace(label) ? adapter : $"{label} ({adapter})") : label ?? key;
            result.Add(new Endpoint($"{{0.0.{flow}.00000000}}.{key}", name));
        }
        return result;
    }
    private static bool IsPrivateHost(string path)
    {
        string normalized = Path.GetFullPath(path);
        return normalized.EndsWith(@"\runtime\qq-call\qq\Files\QQ.exe", StringComparison.OrdinalIgnoreCase);
    }
    private void RefreshChoices()
    {
        targets.Items.Clear();
        foreach (var p in Process.GetProcessesByName("QQ"))
        {
            using (p) { try { string path = p.MainModule?.FileName ?? ""; if (!IsPrivateHost(path)) continue; targets.Items.Add(new Target(p.Id, path, p.StartTime.ToUniversalTime().Ticks)); } catch { } }
        }
        foreach (var pair in new[] { (input, 1, "Steam Streaming Microphone"), (output, 0, "Steam Streaming Speakers") })
        {
            pair.Item1.Items.Clear(); foreach (var item in Devices(pair.Item2)) pair.Item1.Items.Add(item);
            pair.Item1.SelectedItem = pair.Item1.Items.Cast<Endpoint>().FirstOrDefault(item => item.Name.Contains(pair.Item3));
        }
        if (targets.Items.Count > 0) targets.SelectedIndex = 0;
        Write($"Windows {Environment.OSVersion.Version}; QQ 进程 {targets.Items.Count}; 待恢复记录 {saved.Count}；普通 QQ 客户端已过滤");
    }
    private Target Selected() { var t = targets.SelectedItem as Target ?? throw new InvalidOperationException("请选择 Momoi 私有语音宿主进程"); if (!IsPrivateHost(t.Path)) throw new InvalidOperationException("拒绝修改普通 QQ 客户端"); t.Verify(); return t; }
    private void Probe()
    {
        var t = Selected(); using var policy = new Policy();
        foreach (int flow in new[] { 0, 1 }) foreach (int role in new[] { 0, 1, 2 })
            Write($"PID={t.Pid} flow={flow} role={role} override={policy.Get(t.Pid, flow, role)}");
    }
    private void Apply()
    {
        var t = Selected(); using var policy = new Policy();
        var i = input.SelectedItem as Endpoint ?? throw new InvalidOperationException("请选择输入设备");
        var o = output.SelectedItem as Endpoint ?? throw new InvalidOperationException("请选择输出设备");
        if (saved.Any(item => item.Target.Pid == t.Pid)) throw new InvalidOperationException("此目标已有备份，请先恢复再重新应用。");
        var additions = new List<Saved>();
        foreach (int flow in new[] { 0, 1 }) foreach (int role in new[] { 0, 1, 2 })
            additions.Add(new Saved(t, flow, role, policy.Get(t.Pid, flow, role), Policy.Pack(flow == 0 ? o.Id : i.Id, flow)));
        saved.AddRange(additions); Persist();
        try
        {
            foreach (var item in additions)
            {
                t.Verify(); policy.Set(t.Pid, item.Flow, item.Role, item.Desired);
                if (policy.Get(t.Pid, item.Flow, item.Role) != item.Desired) throw new InvalidOperationException("路由回读不匹配");
                Write($"APPLIED PID={t.Pid} flow={item.Flow} role={item.Role} -> {item.Desired}");
            }
            Write("设置回读成功。请重新拨打电话；这不代表音频引擎已遵循设置，需要对端验证。");
        }
        catch { Restore(); throw; }
    }
    private void Persist() { string temp = recovery + ".tmp"; File.WriteAllText(temp, JsonSerializer.Serialize(saved, new JsonSerializerOptions { WriteIndented = true })); File.Move(temp, recovery, true); }
    private void Restore()
    {
        using var policy = new Policy();
        foreach (var item in saved.ToArray().Reverse())
        {
            Target target = item.Target;
            try { target.Verify(); }
            catch
            {
                target = targets.Items.Cast<Target>().FirstOrDefault(t =>
                    string.Equals(t.Path, item.Target.Path, StringComparison.OrdinalIgnoreCase))
                    ?? throw new InvalidOperationException("请先启动原 QQ 应用并刷新进程列表，再恢复保存的应用路由。");
                target.Verify();
            }
            string current = policy.Get(target.Pid, item.Flow, item.Role);
            if (current == item.Desired) { policy.Set(target.Pid, item.Flow, item.Role, item.Previous); if (policy.Get(target.Pid, item.Flow, item.Role) != item.Previous) throw new InvalidOperationException("恢复回读不匹配"); }
            else Write("保留用户之后修改的路由。");
            saved.Remove(item); Persist();
        }
        File.Delete(recovery); Write("原应用路由已恢复。");
    }
}
internal static class Program
{
    [STAThread] private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        if (args.Length == 2 && args[0] == "--devices")
        {
            File.WriteAllText(args[1], JsonSerializer.Serialize(new { inputs = MainForm.Devices(1), outputs = MainForm.Devices(0) }));
            return;
        }
        if (args.Length == 2 && args[0] == "--probe")
        {
            var rows = new List<object>();
            foreach (var process in Process.GetProcessesByName("QQ"))
            {
                using (process) using (var policy = new Policy())
                {
                    foreach (int flow in new[] { 0, 1 }) foreach (int role in new[] { 0, 1, 2 })
                    {
                        try { rows.Add(new { pid = process.Id, flow, role, value = policy.Get(process.Id, flow, role), error = "" }); }
                        catch (Exception e) { rows.Add(new { pid = process.Id, flow, role, value = "", error = $"0x{e.HResult:X8} {e.Message}" }); }
                    }
                }
            }
            File.WriteAllText(args[1], JsonSerializer.Serialize(rows));
            return;
        }
        Application.Run(new MainForm());
    }
}

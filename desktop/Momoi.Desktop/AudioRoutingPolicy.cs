using System;
using System.Runtime.InteropServices;

namespace Momoi.Desktop;

// AudioPolicyConfig ABI used by EarTrumpet; probe supported interfaces at runtime.
// Third-party license: Licenses/EarTrumpet-LICENSE.txt.
internal sealed class Policy : IDisposable
{
    private IntPtr factory;
    [DllImport("combase.dll", CharSet = CharSet.Unicode)] private static extern int WindowsCreateString(string value, uint length, out IntPtr result);
    [DllImport("combase.dll")] private static extern int WindowsDeleteString(IntPtr value);
    [DllImport("combase.dll")] private static extern IntPtr WindowsGetStringRawBuffer(IntPtr value, out uint length);
    [DllImport("combase.dll")] private static extern int RoGetActivationFactory(IntPtr name, ref Guid iid, out IntPtr result);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int Getter(IntPtr self, uint pid, int flow, int role, out IntPtr value);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int Setter(IntPtr self, uint pid, int flow, int role, IntPtr value);
    public Policy()
    {
        const string name = "Windows.Media.Internal.AudioPolicyConfig";
        Marshal.ThrowExceptionForHR(WindowsCreateString(name, (uint)name.Length, out var text));
        try
        {
            // Server editions and serviced builds need capability probing rather than
            // a client Windows build-number assumption. Both interfaces share this ABI.
            string[] variants = { "ab3d4648-e242-459f-b02f-541c70306324", "2a59116d-6c4f-45e0-a74f-707e3fef9258" };
            var failures = new System.Collections.Generic.List<string>();
            foreach (string variant in variants)
            {
                var iid = new Guid(variant);
                int hr = RoGetActivationFactory(text, ref iid, out var candidate);
                if (hr >= 0 && candidate != IntPtr.Zero) { factory = candidate; return; }
                if (candidate != IntPtr.Zero) Marshal.Release(candidate);
                failures.Add($"iid={variant} HRESULT=0x{hr:X8}");
            }
            throw new NotSupportedException($"Windows 应用音频路由接口不可用；OS={Environment.OSVersion.Version}; {string.Join("; ", failures)}。QQ 音频路由尚未应用。");
        }
        finally { WindowsDeleteString(text); }
    }
    private T Method<T>(int index) where T : Delegate => Marshal.GetDelegateForFunctionPointer<T>(Marshal.ReadIntPtr(Marshal.ReadIntPtr(factory), index * IntPtr.Size));
    public string Get(int pid, int flow, int role)
    {
        int hr = Method<Getter>(26)(factory, (uint)pid, flow, role, out var value);
        // Missing preference is distinct from invalid argument; never guess on failed reads.
        if (hr == unchecked((int)0x80070490)) return "";
        Marshal.ThrowExceptionForHR(hr);
        try { var pointer = WindowsGetStringRawBuffer(value, out uint length); return pointer == IntPtr.Zero ? "" : Marshal.PtrToStringUni(pointer, (int)length) ?? ""; }
        finally { WindowsDeleteString(value); }
    }
    public void Set(int pid, int flow, int role, string value)
    {
        IntPtr text = IntPtr.Zero;
        try
        {
            if (value.Length > 0) Marshal.ThrowExceptionForHR(WindowsCreateString(value, (uint)value.Length, out text));
            Marshal.ThrowExceptionForHR(Method<Setter>(25)(factory, (uint)pid, flow, role, text));
        }
        finally { WindowsDeleteString(text); }
    }
    public static string Pack(string id, int flow) => @"\\?\SWD#MMDEVAPI#" + id + (flow == 0 ? "#{e6327cad-dcec-4949-ae8a-991e976a79d2}" : "#{2eef81be-33fa-4800-9670-1cd474972c3f}");
    public void Dispose() { if (factory != IntPtr.Zero) { Marshal.Release(factory); factory = IntPtr.Zero; } }
}

namespace Momoi.Update;

/// <summary>Release infrastructure is part of the native shell's trust boundary.</summary>
public static class UpdateSource
{
    // No environment variable, user configuration or command-line override.
    public const string LatestManifestUrl = "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/latest.json";
}

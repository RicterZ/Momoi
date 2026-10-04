namespace Momoi.Update;

/// <summary>Release infrastructure is part of the native shell's trust boundary.</summary>
public static class UpdateSource
{
    // TODO: set the owner's actual COS HTTPS latest.json URL before distributing.
    // No environment variable, user configuration or command-line override.
    public const string LatestManifestUrl = "";
}

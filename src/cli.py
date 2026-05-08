#!/usr/bin/env python3
"""CLI entry point for fictional cartography."""
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description="Fictional Cartography Map Renderer")
    subparsers = parser.add_subparsers(dest="command")

    # Render command
    render_p = subparsers.add_parser("render", help="Render a map from config")
    render_p.add_argument("config", help="Path to YAML config file")
    render_p.add_argument("--output", "-o", help="Output path (default: output/<config-name>.<ext>)")
    render_p.add_argument("--scale", "-s", type=int, default=1, help="Scale factor (1=config downsample, 4=hi-res)")
    render_p.add_argument(
        "--format", "-f",
        choices=["png", "jpg", "jpeg", "webp", "tif", "tiff", "pdf"],
        default="png",
        help="Output format (png/jpg/webp = web; tiff/pdf = print)",
    )
    render_p.add_argument("--quality", "-q", type=int, default=88, help="JPEG/WebP quality (60-100)")
    render_p.add_argument("--reference", "-r", action="store_true", help="Enable reference overlays (state borders, cities)")
    render_p.add_argument(
        "--dpi", type=int, default=None,
        help="Embed physical-DPI hint in the file (72-1200). Useful for print formats.",
    )
    render_p.add_argument(
        "--embed-metadata", action="store_true",
        help="Embed map name/subtitle/credit + render timestamp into the file metadata.",
    )

    # List command
    list_p = subparsers.add_parser("list", help="List available map configs")

    # Fetch command  
    fetch_p = subparsers.add_parser("fetch", help="Pre-fetch data for a map config")
    fetch_p.add_argument("config", help="Path to YAML config file")

    args = parser.parse_args()

    if args.command == "render":
        from src.pipeline import render_map
        render_map(
            config_path=args.config,
            output_path=args.output,
            scale_factor=args.scale,
            output_format=args.format,
            jpeg_quality=args.quality,
            reference_mode=args.reference,
            dpi=args.dpi,
            embed_metadata=args.embed_metadata,
        )
    elif args.command == "list":
        import glob, os
        configs = glob.glob("config/*.yaml")
        configs = [c for c in configs if not c.endswith("_template.yaml")]
        if configs:
            print("Available map configs:")
            for c in sorted(configs):
                name = os.path.basename(c).replace(".yaml", "")
                print(f"  {name:30s} ({c})")
        else:
            print("No map configs found in config/")
    elif args.command == "fetch":
        from src.pipeline import fetch_data
        fetch_data(args.config)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()

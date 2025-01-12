#!/usr/bin/env python3

import argparse
import logging
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from typing import Dict

logger = logging.getLogger(__name__)


def read_excel_data(excel_path: str) -> Dict[str, pd.DataFrame]:
    """Read all required sheets from the Excel file."""
    data = {}

    # Read named sheets
    sheet_names = {
        "overall_performance": "Overall Performance",
        "layer_metrics": "Layer Metrics",
        "energy_analysis": "Energy Analysis",
    }

    for key, sheet_name in sheet_names.items():
        try:
            data[key] = pd.read_excel(excel_path, sheet_name=sheet_name)
            logger.debug(f"Successfully read sheet '{sheet_name}'")
        except Exception as e:
            print(f"Warning: Could not read sheet '{sheet_name}': {e}")
            data[key] = None

    return data


def validate_dataframe(df: pd.DataFrame, required_cols: list, sheet_name: str) -> None:
    """Validate that DataFrame contains required columns."""
    if not all(col in df.columns for col in required_cols):
        raise ValueError(
            f"Excel sheet '{sheet_name}' must contain columns: {required_cols}"
        )


def plot_layer_metrics_tab(
    df: pd.DataFrame, split_df: pd.DataFrame, output_path: str
) -> None:
    """Create visualization for 'Layer Metrics' tab with per-layer latency and output size."""
    # Validate required columns
    required_cols = [
        "Split Layer",
        "Layer ID",
        "Layer Type",
        "Layer Latency (ms)",
        "Output Size (MB)",
    ]
    validate_dataframe(df, required_cols, "Layer Metrics")

    # Validate split_df columns
    required_split_cols = [
        "Split Layer Index",
        "Host Time",
        "Travel Time",
        "Server Time",
        "Total Processing Time",
    ]
    validate_dataframe(split_df, required_split_cols, "Overall Performance")

    # Set style
    _set_plot_style()

    # Create figure
    fig, ax1 = plt.subplots(figsize=(8, 3))
    ax2 = ax1.twinx()
    ax3 = ax1.twinx()
    ax3.spines["right"].set_position(("outward", 50))

    # Extract layer number from Layer ID
    def extract_layer_num(layer_id):
        if isinstance(layer_id, str):
            import re

            match = re.search(r"\d+", layer_id)
            return int(match.group()) if match else 0
        elif isinstance(layer_id, (int, float)):
            return int(layer_id)
        else:
            return 0

    # Process layer metrics
    df["Layer Num"] = df["Layer ID"].apply(extract_layer_num)
    grouped = (
        df.groupby("Layer Num")
        .agg(
            {
                "Layer Type": "first",
                "Layer Latency (ms)": "mean",
                "Output Size (MB)": "mean",
            }
        )
        .reset_index()
    )
    grouped = grouped.sort_values("Layer Num")

    # Set bar width and positions
    bar_width = 0.25
    x = np.arange(len(grouped))

    # Color scheme (colorblind-friendly)
    color_latency = "#a1c9f4"  # Light blue
    color_size = "#2c3e50"  # Dark blue
    color_time = "#8b0000"  # Dark red for total time

    # Plot layer metrics
    latency_bars = ax1.bar(
        x - bar_width / 2,
        grouped["Layer Latency (ms)"],
        bar_width,
        label="Layer latency",
        color=color_latency,
        edgecolor="black",
        linewidth=0.5,
    )

    size_bars = ax2.bar(
        x + bar_width / 2,
        grouped["Output Size (MB)"],
        bar_width,
        label="Output size",
        color=color_size,
        edgecolor="black",
        linewidth=0.5,
    )

    # Calculate and plot total processing time
    x_time = np.arange(len(split_df))
    total_time_line = ax3.plot(
        x_time,
        split_df["Total Processing Time"],
        color=color_time,
        linestyle="-",
        linewidth=1,
        label="Total processing time",
    )

    # Find optimal split
    optimal_idx = split_df["Total Processing Time"].idxmin()
    optimal_time = split_df["Total Processing Time"].min()

    # Add vertical line for optimal split
    ax1.axvline(
        x=optimal_idx, color=color_time, linestyle="--", linewidth=0.8, alpha=0.4
    )

    # Add grid
    ax1.grid(True, linestyle=":", alpha=0.3, color="gray")
    ax1.set_axisbelow(True)

    # Customize axes
    ax1.set_ylabel("Layer latency (ms)")
    ax2.set_ylabel("Output size (MB)")
    ax3.set_ylabel("Total processing time (s)", color=color_time)
    ax3.tick_params(axis="y", labelcolor=color_time)

    # Set axis limits and ticks with specific increments
    max_latency = max(grouped["Layer Latency (ms)"])
    max_size = max(grouped["Output Size (MB)"])
    max_total_time = max(split_df["Total Processing Time"])

    # Left y-axis (Latency): increments of 10
    max_latency_rounded = np.ceil(max_latency / 10) * 10
    latency_ticks = np.arange(0, max_latency_rounded + 10, 10)
    ax1.set_ylim(0, max_latency_rounded)
    ax1.set_yticks(latency_ticks)

    # Middle y-axis (Output size): increments of 0.3
    max_size_rounded = np.ceil(max_size / 0.3) * 0.3
    size_ticks = np.arange(0, max_size_rounded + 0.3, 0.3)
    ax2.set_ylim(0, max_size_rounded)
    ax2.set_yticks(size_ticks)

    # Right y-axis (Total time): adaptive increments
    if max_total_time < 50:  # YOLOv8 case
        increment = 5
    else:  # ResNet case
        increment = 30

    max_time_rounded = np.ceil(max_total_time / increment) * increment
    time_ticks = np.arange(0, max_time_rounded + increment, increment)
    ax3.set_ylim(0, max_time_rounded)
    ax3.set_yticks(time_ticks)

    # Set x-axis labels
    ax1.set_xticks(x)
    ax1.set_xticklabels(
        grouped["Layer Type"], rotation=90, ha="center", va="top", fontsize=7
    )

    # Add subtle annotation for optimal split
    min_time_text = f"(min: {optimal_time:.2f}s)"
    ax3.annotate(
        min_time_text,
        xy=(optimal_idx, optimal_time),
        xytext=(5, 5),
        textcoords="offset points",
        fontsize=7,
        color=color_time,
        alpha=0.7,
        bbox=dict(facecolor="white", edgecolor="none", alpha=0.8),
    )

    # Add legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    lines3, labels3 = ax3.get_legend_handles_labels()
    ax1.legend(
        lines1 + lines2 + lines3,
        labels1 + labels2 + labels3,
        loc="upper right",
        frameon=True,
        framealpha=0.9,
        edgecolor="none",
        ncol=3,
        columnspacing=1,
        handletextpad=0.5,
        borderaxespad=0.5,
    )

    # Clean up spines
    ax1.spines["top"].set_visible(False)
    ax2.spines["top"].set_visible(False)
    ax3.spines["top"].set_visible(False)

    # Adjust layout and save
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close()


def plot_overall_performance_tab(df: pd.DataFrame, output_path: str) -> None:
    """Create visualization for 'Overall Performance' tab with stacked latency bars."""
    # Validate required columns
    required_cols = [
        "Split Layer Index",
        "Host Time",
        "Travel Time",
        "Server Time",
        "Total Processing Time",
    ]
    validate_dataframe(df, required_cols, "Overall Performance")

    # Set style
    _set_plot_style()

    # Create figure with extra space at top for legend
    fig, ax = plt.subplots(figsize=(8, 3))

    # Colors matching the reference plot
    colors = [
        "#4a6fa5",  # Server processing (dark blue)
        "#93b7be",  # Data communication (medium blue)
        "#c7dbe6",  # Mobile processing (light blue)
    ]

    # Plot stacked bars
    x = np.arange(len(df))
    bottom = np.zeros(len(df))
    metrics = ["Server Time", "Travel Time", "Host Time"]
    labels = ["Server processing", "Data communication", "Mobile processing"]

    for metric, color, label in zip(metrics, colors, labels):
        ax.bar(
            x,
            df[metric],
            bottom=bottom,
            color=color,
            edgecolor="black",
            linewidth=0.5,
            label=label,
            width=0.65,
        )
        bottom += df[metric]

    # Customize axes
    ax.set_ylabel("Latency (s)")
    ax.set_xticks(x)
    ax.set_xticklabels(
        df["Split Layer Index"], rotation=90, ha="center", va="top", fontsize=7
    )

    # Set y-axis limits and ticks
    y_max = 35
    ax.set_ylim(0, y_max)
    major_ticks = np.arange(0, y_max + 5, 5)
    ax.set_yticks(major_ticks)
    ax.set_yticklabels([f"{x:.0f}" for x in major_ticks])

    # Clean up plot
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)
    ax.yaxis.grid(True, linestyle="-", alpha=0.08, color="gray", zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", which="both", right=False)

    # Add legend at the top of the plot
    legend = ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, 1.15),
        ncol=3,
        frameon=False,
        handletextpad=0.3,
        columnspacing=1.0,
        fontsize=7,
    )

    # Find the bar with minimum total latency
    total_latencies = df["Server Time"] + df["Travel Time"] + df["Host Time"]
    best_idx = total_latencies.idxmin()
    best_latency = total_latencies[best_idx]

    # Calculate consistent spacing (3 units between elements)
    spacing = 3
    star_height = best_latency + 12
    text_height = star_height - spacing
    arrow_start = text_height - spacing

    # Add star at the top
    ax.plot(best_idx, star_height, marker="*", markersize=10, color="#ffd700", zorder=5)

    # Add "Best latency" text below the star
    ax.text(best_idx, text_height, "Best latency", ha="center", va="bottom", fontsize=7)

    # Add arrow at the bottom
    ax.annotate(
        "",
        xy=(best_idx, best_latency),  # arrow tip at bar top
        xytext=(best_idx, arrow_start),  # arrow starts higher
        ha="center",
        va="bottom",
        arrowprops=dict(
            arrowstyle="-|>", color="black", linewidth=1.5, mutation_scale=12
        ),
    )

    # Save plot
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close()


def plot_raw_power_metrics_tab(df: pd.DataFrame, output_path: str) -> None:
    """Create visualization for 'Raw Power Metrics' tab showing CPU, memory, and battery usage."""
    # Set style
    _set_plot_style()

    # Create figure
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), height_ratios=[1, 1])

    # Plot CPU metrics
    cpu_cols = [col for col in df.columns if "cpu" in col.lower()]
    for col in cpu_cols:
        ax1.plot(df.index, df[col], label=col, linewidth=1)

    ax1.set_ylabel("CPU Usage (%)")
    ax1.grid(True, alpha=0.08, color="gray")
    ax1.legend(loc="upper right", ncol=2, fontsize=7)

    # Plot memory and battery metrics
    mem_cols = [col for col in df.columns if "memory" in col.lower()]
    bat_cols = [col for col in df.columns if "battery" in col.lower()]

    for col in mem_cols + bat_cols:
        ax2.plot(df.index, df[col], label=col, linewidth=1)

    ax2.set_ylabel("Usage")
    ax2.set_xlabel("Time")
    ax2.grid(True, alpha=0.08, color="gray")
    ax2.legend(loc="upper right", ncol=2, fontsize=7)

    # Clean up spines
    for ax in [ax1, ax2]:
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.tick_params(axis="both", which="both", labelsize=7)

    # Save plot with consistent padding
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close()


def plot_layer_energy_metrics_tab(df: pd.DataFrame, output_path: str) -> None:
    """Create visualization for layer-specific energy metrics."""
    # Validate required columns
    required_cols = [
        "Split Layer",
        "Layer ID",
        "Layer Type",
        "Processing Energy (J)",
        "Communication Energy (J)",
        "Power Reading (W)",
        "GPU Utilization (%)",
        "Total Energy (J)",
    ]
    validate_dataframe(df, required_cols, "Layer Metrics")

    # Set style
    _set_plot_style()

    # Create figure with two subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), height_ratios=[1.2, 1])

    # Plot energy metrics in first subplot
    x = range(len(df))
    bar_width = 0.35

    # Colors (colorblind-friendly)
    color_proc = "#4a6fa5"  # Dark blue for processing
    color_comm = "#93b7be"  # Light blue for communication
    color_total = "#2c3e50"  # Dark gray for total

    # Create stacked bars for energy metrics
    ax1.bar(
        x,
        df["Processing Energy (J)"],
        bar_width,
        label="Processing Energy",
        color=color_proc,
    )
    ax1.bar(
        x,
        df["Communication Energy (J)"],
        bar_width,
        bottom=df["Processing Energy (J)"],
        label="Communication Energy",
        color=color_comm,
    )

    # Add total energy line
    ax1_twin = ax1.twinx()
    ax1_twin.plot(
        x,
        df["Total Energy (J)"],
        color=color_total,
        linestyle="--",
        label="Total Energy",
        linewidth=1.5,
        marker="o",
    )

    # Customize first subplot
    ax1.set_ylabel("Energy (J)")
    ax1_twin.set_ylabel("Total Energy (J)")
    ax1.grid(True, alpha=0.15)

    # Add layer type labels
    plt.xticks(x, df["Layer Type"], rotation=45, ha="right")

    # Combine legends from both y-axes
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1_twin.get_legend_handles_labels()
    ax1.legend(
        lines1 + lines2,
        labels1 + labels2,
        loc="upper left",
        ncol=3,
        bbox_to_anchor=(0, 1.15),
    )

    # Plot GPU metrics in second subplot
    color_power = "#c0504d"  # Red for power
    color_gpu = "#9bbb59"  # Green for GPU

    # Create line plots for power and GPU utilization
    ax2.plot(
        x,
        df["Power Reading (W)"],
        color=color_power,
        label="Power Reading",
        linewidth=1.5,
        marker="o",
    )
    ax2_twin = ax2.twinx()
    ax2_twin.plot(
        x,
        df["GPU Utilization (%)"],
        color=color_gpu,
        label="GPU Utilization",
        linewidth=1.5,
        marker="s",
    )

    # Customize second subplot
    ax2.set_xlabel("Layer Type")
    ax2.set_ylabel("Power (W)")
    ax2_twin.set_ylabel("GPU Utilization (%)")
    ax2.grid(True, alpha=0.15)
    plt.xticks(x, df["Layer Type"], rotation=45, ha="right")

    # Combine legends for second subplot
    lines3, labels3 = ax2.get_legend_handles_labels()
    lines4, labels4 = ax2_twin.get_legend_handles_labels()
    ax2.legend(lines3 + lines4, labels3 + labels4, loc="upper left", ncol=2)

    # Add split layer markers
    split_layers = df[df["Split Layer"] == 1].index
    for split_idx in split_layers:
        ax1.axvline(x=split_idx, color="red", linestyle=":", alpha=0.5)
        ax2.axvline(x=split_idx, color="red", linestyle=":", alpha=0.5)

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close()


def plot_energy_analysis_tab(df: pd.DataFrame, output_path: str) -> None:
    """Create visualization for energy analysis showing metrics per split point."""
    # Update required columns to match new format
    required_cols = [
        "Split Layer",
        "Processing Energy (J)",
        "Communication Energy (J)",
        "Total Energy (J)",
        "Power Reading (W)",
        "GPU Utilization (%)",
    ]
    validate_dataframe(df, required_cols, "Energy Analysis")

    # Set style
    _set_plot_style()

    # Create figure with two subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), height_ratios=[1, 1])

    # Plot energy metrics in first subplot
    x = df["Split Layer"]
    bar_width = 0.35

    # Colors (colorblind-friendly)
    color_proc = "#4a6fa5"  # Dark blue for processing
    color_comm = "#93b7be"  # Light blue for communication
    color_total = "#2c3e50"  # Dark gray for total

    # Create grouped bars for energy metrics
    ax1.bar(
        x - bar_width / 2,
        df["Processing Energy (J)"],
        bar_width,
        label="Processing Energy",
        color=color_proc,
    )
    ax1.bar(
        x + bar_width / 2,
        df["Communication Energy (J)"],
        bar_width,
        label="Communication Energy",
        color=color_comm,
    )

    # Add total energy line
    ax1_twin = ax1.twinx()
    ax1_twin.plot(
        x,
        df["Total Energy (J)"],
        color=color_total,
        linestyle="--",
        label="Total Energy",
        linewidth=1.5,
        marker="o",
    )

    # Customize first subplot
    ax1.set_ylabel("Energy (J)")
    ax1_twin.set_ylabel("Total Energy (J)")
    ax1.grid(True, alpha=0.15)

    # Combine legends from both y-axes
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1_twin.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", ncol=3)

    # Plot GPU metrics in second subplot
    color_power = "#c0504d"  # Red for power
    color_gpu = "#9bbb59"  # Green for GPU

    # Create line plots for power and GPU utilization
    ax2.plot(
        x,
        df["Power Reading (W)"],
        color=color_power,
        label="Power Reading",
        linewidth=1.5,
        marker="o",
    )
    ax2_twin = ax2.twinx()
    ax2_twin.plot(
        x,
        df["GPU Utilization (%)"],
        color=color_gpu,
        label="GPU Utilization",
        linewidth=1.5,
        marker="s",
    )

    # Customize second subplot
    ax2.set_xlabel("Split Layer")
    ax2.set_ylabel("Power (W)")
    ax2_twin.set_ylabel("GPU Utilization (%)")
    ax2.grid(True, alpha=0.15)

    # Set integer ticks for x-axis
    for ax in [ax1, ax2]:
        ax.set_xticks(x)
        ax.set_xticklabels([f"{int(i)}" for i in x])

    # Combine legends from both y-axes for second subplot
    lines3, labels3 = ax2.get_legend_handles_labels()
    lines4, labels4 = ax2_twin.get_legend_handles_labels()
    ax2.legend(lines3 + lines4, labels3 + labels4, loc="upper left", ncol=2)

    # Find and mark minimum total energy point
    min_energy_idx = df["Total Energy (J)"].idxmin()
    min_energy_split = df.iloc[min_energy_idx]["Split Layer"]
    min_energy_value = df.iloc[min_energy_idx]["Total Energy (J)"]

    ax1_twin.plot(
        min_energy_split,
        min_energy_value,
        "r*",
        markersize=10,
        label=f"Min Energy\n(Split {int(min_energy_split)})",
    )
    ax1_twin.annotate(
        f"Min: {min_energy_value:.3f}J",
        (min_energy_split, min_energy_value),
        xytext=(10, 10),
        textcoords="offset points",
        fontsize=7,
        bbox=dict(facecolor="white", edgecolor="none", alpha=0.8),
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close()


def _set_plot_style() -> None:
    """Set consistent plot style across all visualizations."""
    plt.style.use("seaborn-v0_8-paper")
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.size": 8,  # Base font size
            "axes.labelsize": 8,  # Axis label size
            "axes.titlesize": 8,  # Title size
            "xtick.labelsize": 7,  # X-tick label size
            "ytick.labelsize": 7,  # Y-tick label size
            "legend.fontsize": 7,  # Legend font size
            "figure.dpi": 300,
            "axes.grid": False,  # No grid by default
            "grid.alpha": 0.08,  # Consistent grid transparency
            "grid.color": "gray",  # Consistent grid color
            "grid.linestyle": "-",  # Consistent grid style
        }
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate visualizations from Excel metrics"
    )
    parser.add_argument("excel_path", help="Path to the Excel file containing metrics")
    parser.add_argument(
        "--output-dir",
        "-o",
        default=".",
        help="Output directory for plots (default: current directory)",
    )
    parser.add_argument(
        "--plot-type",
        "-t",
        choices=["layer_metrics", "overall_performance", "energy_analysis", "all"],
        default="all",
        help="Type of plot to generate (default: all)",
    )

    args = parser.parse_args()

    try:
        # Create output directory
        import os

        os.makedirs(args.output_dir, exist_ok=True)

        # Read all data at once
        data = read_excel_data(args.excel_path)

        # Generate requested plots
        if args.plot_type in ["layer_metrics", "all"]:
            if data["layer_metrics"] is not None:
                # Plot layer energy metrics
                output_path = os.path.join(args.output_dir, "layer_energy_metrics.png")
                plot_layer_energy_metrics_tab(data["layer_metrics"], output_path)
                print(f"Layer energy metrics plot saved to: {output_path}")

                # Plot other layer metrics
                output_path = os.path.join(args.output_dir, "layer_metrics.png")
                plot_layer_metrics_tab(
                    data["layer_metrics"], data["overall_performance"], output_path
                )
                print(f"Layer metrics plot saved to: {output_path}")
            else:
                print(
                    "Warning: Could not generate layer metrics plots - required sheets not found"
                )

        if args.plot_type in ["overall_performance", "all"]:
            if data["overall_performance"] is not None:
                output_path = os.path.join(args.output_dir, "overall_performance.png")
                plot_overall_performance_tab(data["overall_performance"], output_path)
                print(f"Overall performance plot saved to: {output_path}")
            else:
                print(
                    "Warning: Could not generate overall performance plot - required sheet not found"
                )

        if args.plot_type in ["energy_analysis", "all"]:
            if data["energy_analysis"] is not None:
                output_path = os.path.join(args.output_dir, "energy_analysis.png")
                plot_energy_analysis_tab(data["energy_analysis"], output_path)
                print(f"Energy analysis plot saved to: {output_path}")
            else:
                print(
                    "Warning: Could not generate energy analysis plot - required sheet not found"
                )

    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())

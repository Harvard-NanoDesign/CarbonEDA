# Render one png per step (plus one per cut plane) from the tdr files of an spx run.
# Author: David Kong
# License: MIT
#
# Not run directly: carbon_eda writes outputs/<tech>/img/render_job.tcl, which sets
#   job_steps   list of {tdr title stem ?zoom_box? ?cuts?}; tdr may be a .tdr.zst; a step's own
#               zoom_box and cuts (when not empty) replace the ones below
#   img_dir     output directory
#   tmp_dir     where a .tdr.zst is decompressed for its step (removed after it)
#   zstd        zstd executable
#   resolution  e.g. 1024x768
#   fit_resolution  e.g. 640x480, for the zoom-to-fit view
#   zoom_box    {} or {x0 x1 y0 y1 z0 z1} (um)
#   cuts        list of {axis at ranges resolution}; ranges is a list of {axis lo hi},
#               applied in order (svisual keeps the aspect ratio, so the last range
#               wins unless resolution, e.g. 600x768, matches the ranges' aspect);
#               an empty resolution uses $resolution
#   material_colors  dict material -> #rrggbb (the spx colors); other materials keep svisual's
# and then sources this file. Run with: svisual -tcl render_job.tcl (or -batchx)
# Writes <img_dir>/<stem>.3d.png (at zoom_box, the same on every step), <img_dir>/<stem>.fit.png
# (zoomed to fit this step's structure) and <img_dir>/<stem>.cut<i>-<axis>.png.

if { [llength [list_datasets]] } { remove_datasets [list_datasets] }

set n_done 0
set n_jobs [llength $job_steps]
set i_job 0
foreach job $job_steps {
    lassign $job tdr title stem step_zoom step_cuts
    set box [expr { [llength $step_zoom] ? $step_zoom : $zoom_box }]
    set step_cut_list [expr { [llength $step_cuts] ? $step_cuts : $cuts }]
    incr i_job
    if { ![file exists $tdr] } {
        puts "skip (no tdr): $tdr"
        continue
    }
    puts "[clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}]  rendering $i_job/$n_jobs: $stem"
    flush stdout
    # svisual reads only plain tdr: decompress this one step's
    set plain ""
    if { [file extension $tdr] eq ".zst" } {
        file mkdir $tmp_dir
        set plain [file join $tmp_dir [file rootname [file tail $tdr]]]
        if { [catch { exec $zstd -d -qq -f $tdr -o $plain 2>@1 } err] } {
            puts "skip (zstd failed: $err): $tdr"
            file delete -force $plain
            continue
        }
        set tdr $plain
    }
    # one dataset at a time: a full flow has hundreds of tdr files
    set ds [load_file $tdr -fod -name $stem]
    set plot [create_plot -dataset $ds -name $stem]
    # no title: the report names each step, and a title can cover a tall structure
    set_plot_prop -plot $plot -hide_title
    # spx's colors; the cut plots take them from this plot
    foreach m [list_materials -plot $plot] {
        if { [info exists material_colors] && [dict exists $material_colors $m] } {
            set_material_prop [list $m] -plot $plot -color [dict get $material_colors $m]
        }
    }
    # svisual's own view fits this step's structure; then the zoom shared by every step
    export_view [file join $img_dir "$stem.fit.png"] -plots $plot -format png -resolution $fit_resolution -overwrite
    if { [llength $box] } { zoom_plot -plot $plot -box $box }
    export_view [file join $img_dir "$stem.3d.png"] -plots $plot -format png -resolution $resolution -overwrite

    set i 0
    foreach cut $step_cut_list {
        lassign $cut axis at ranges cut_res
        if { $cut_res eq "" } { set cut_res $resolution }
        set cp [create_cutplane -plot $plot -type $axis -at $at]
        set cplot [create_plot -dataset $cp -ref_plot $plot]
        foreach r $ranges {
            lassign $r ax lo hi
            set_axis_prop -plot $cplot -axis $ax -range [list $lo $hi]
        }
        # the report titles each cut; a title would crowd a narrow cut image
        set_plot_prop -plot $cplot -hide_title
        export_view [file join $img_dir "$stem.cut$i-$axis.png"] -plots $cplot -format png -resolution $cut_res -overwrite
        # svisual tends to crash with several cut plots open at once
        remove_plots $cplot
        incr i
    }
    remove_plots $plot
    remove_datasets [list_datasets]
    if { $plain ne "" } { file delete -force $plain }
    incr n_done
}
puts "rendered $n_done of [llength $job_steps] steps"
exit

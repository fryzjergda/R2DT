#!/usr/bin/env python3

"""
Copyright [2009-present] EMBL-European Bioinformatics Institute
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
     http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import glob
import json
import os
import re
import shutil
import subprocess as sp

import click  # pylint: disable=import-error
from colorhash import ColorHash  # pylint: disable=import-error

from tests import tests
from utils import config, core
from utils import generate_cm_library as gcl
from utils import generate_model_info as gmi
from utils import gtrnadb
from utils import list_models as lm
from utils import rfam, shared


def get_ribotyper_output(fasta_input, output_folder, cm_library, skip_ribovore_filters):
    """
    Run ribotyper on the fasta sequences to select the best matching covariance
    model.
    """
    ribotyper_long_out = os.path.join(
        output_folder, os.path.basename(output_folder) + ".ribotyper.long.out"
    )
    if not os.path.exists(ribotyper_long_out):
        cmd = (
            f"ribotyper.pl --skipval -i {cm_library}/modelinfo.txt "
            f"-f {fasta_input} {output_folder}"
        )
        print(cmd)
        os.system(cmd)
    f_out = os.path.join(output_folder, "hits.txt")
    if not skip_ribovore_filters:
        cmd = (
            f"cat {ribotyper_long_out} | grep -v '^#' | "
            f"grep -v MultipleHits | grep PASS | "
            f"awk -v OFS='\t' '{{print $2, $8, $3}}' > {f_out}"
        )
    else:
        cmd = (
            f"cat {ribotyper_long_out} | grep -v '^#' "
            f"| grep -v NoHits | "
            f"awk -v OFS='\t' '{{print $2, $8, $3}}' > {f_out}"
        )
    os.system(cmd)
    return f_out


@click.group()
def cli():
    """Required click stub function."""


@cli.command()
def version():
    """
    Print R2DT version information.
    """
    print(shared.get_r2dt_version_header())


@cli.command()
def setup():
    """
    Generate all templates from scratch.
    """
    print(shared.get_r2dt_version_header())
    if not os.path.exists(config.CM_LIBRARY):
        os.makedirs(config.CM_LIBRARY)
    crw_setup()
    rfam.setup()
    gtrnadb.setup()


def crw_setup():
    """Setup CRW CM library."""
    print("Deleting old CRW library")
    os.system(f"rm -Rf {config.CRW_CM_LIBRARY}")
    print("Extracting precomputed CRW archive")
    cmd = ["tar", "xf", "crw-cms.tar.gz"]
    sp.check_output(cmd, cwd=config.DATA)
    cmd = ["mv", "crw-cms", os.path.join(config.CM_LIBRARY, "crw")]
    sp.check_output(cmd, cwd=config.DATA)
    print("Generating CRW modelinfo file")
    gmi.generate_model_info(cm_library=config.CRW_CM_LIBRARY)


@cli.command()
def setup_rfam():
    """
    Re-generate Rfam templates from scratch.
    """
    print(shared.get_r2dt_version_header())
    # delete Rfam cms
    rfam_cms = os.path.join(config.CM_LIBRARY, "rfam")
    os.system(f"rm -f {rfam_cms}/*.cm")
    os.system(f"rm -f {rfam_cms}/modelinfo.txt")
    # delete template files
    os.system(f"rm -Rf {config.RFAM_DATA}/RF0*")
    # delete summary files
    os.system(f"rm -Rf {config.RFAM_DATA}/family.txt")
    os.system(f"rm -Rf {config.RFAM_DATA}/rfam_ids.txt")
    # run setup
    rfam.setup()
    # delete temporary files
    os.system(f"cd {config.RFAM_DATA} && ./clean_up_files.sh")


def get_seq_ids(input_fasta):
    """
    Get a list of sequence ids from a fasta file.
    """
    seq_ids = set()
    with open(input_fasta, "r", encoding="utf-8") as f_in:
        for line in f_in:
            if line.startswith(">"):
                match = re.search(r">(.*?)\s", line)
                if match:
                    seq_ids.add(match.group(1))
    return seq_ids


def get_hits(folder):
    """
    Get a list of sequence ids found in the hits.txt file by ribovore.
    """
    hits = set()
    hits_file = os.path.join(folder, "hits.txt")
    if not os.path.exists(hits_file):
        return hits
    with open(hits_file, "r", encoding="utf-8") as f_in:
        for line in f_in:
            hits.add(line.split("\t")[0])
    return hits


def get_subset_fasta(fasta_input, output_filename, seq_ids):
    """
    Extract a fasta file named <output_filename> with sequence ids <seq_ids>
    from <fasta_input>.
    """
    index_filename = output_filename + ".txt"
    with open(index_filename, "w", encoding="utf-8") as f_out:
        for seq_id in seq_ids:
            f_out.write(f"{seq_id}\n")
    cmd = f"esl-sfetch -o {output_filename} -f {fasta_input} {index_filename}"
    os.system(cmd)
    os.system("esl-sfetch --index " + output_filename)


@cli.command()
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
@click.option(
    "--force_template",
    type=click.STRING,
    default=None,
    help="Force sequences into a specific template",
)
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.option(
    "--skip_ribovore_filters",
    default=False,
    is_flag=True,
    help="Ignore ribovore QC checks",
)
@click.pass_context
# pylint: disable-next=too-many-arguments, too-many-locals, too-many-statements
def draw(
    ctx,
    fasta_input,
    output_folder,
    force_template,
    constraint,
    exclusion,
    fold_type,
    skip_ribovore_filters,
):
    """
    Single entry point for visualising 2D for an RNA sequence.
    Selects a template and runs Traveler using CRW, LSU, or Rfam libraries.
    """
    print(shared.get_r2dt_version_header())
    all_seq_ids = get_seq_ids(fasta_input)

    if force_template:
        for seq_id in all_seq_ids:
            force_draw(
                force_template,
                fasta_input,
                output_folder,
                seq_id,
                constraint,
                exclusion,
                fold_type,
            )
        return

    os.system(f"mkdir -p {output_folder}")
    crw_output = os.path.join(output_folder, "crw")
    ribovision_ssu_output = os.path.join(output_folder, "ribovision-ssu")
    ribovision_lsu_output = os.path.join(output_folder, "ribovision-lsu")
    rfam_output = os.path.join(output_folder, "rfam")
    gtrnadb_output = os.path.join(output_folder, "gtrnadb")
    rfam_trna_output = os.path.join(output_folder, "RF00005")
    rnasep_output = os.path.join(output_folder, "rnasep")

    hits = set()
    subset_fasta = os.path.join(output_folder, "subset.fasta")
    os.system(f"cp {fasta_input} {subset_fasta}")
    os.system("esl-sfetch --index " + subset_fasta)

    # Rfam
    print(f"Analysing {len(all_seq_ids)} sequences with Rfam")
    with open(
        get_ribotyper_output(
            fasta_input,
            rfam_output,
            os.path.join(config.CM_LIBRARY, "rfam"),
            skip_ribovore_filters,
        ),
        "r",
        encoding="utf-8",
    ) as f_ribotyper:
        for line in f_ribotyper.readlines():
            rnacentral_id, model_id, _ = line.split("\t")
            core.visualise(
                "rfam",
                fasta_input,
                rfam_output,
                rnacentral_id,
                model_id,
                constraint,
                exclusion,
                fold_type,
            )

    # RiboVision SSU
    hits = hits.union(get_hits(rfam_output))
    subset = all_seq_ids.difference(hits)
    if subset:
        get_subset_fasta(fasta_input, subset_fasta, subset)
        print(f"Analysing {len(subset)} sequences with RiboVision SSU")
        ctx.invoke(
            ribovision_draw_ssu,
            fasta_input=subset_fasta,
            output_folder=ribovision_ssu_output,
            constraint=constraint,
            exclusion=exclusion,
            fold_type=fold_type,
            skip_ribovore_filters=skip_ribovore_filters,
        )

    # CRW
    hits = hits.union(get_hits(ribovision_ssu_output))
    subset = all_seq_ids.difference(hits)
    if subset:
        get_subset_fasta(fasta_input, subset_fasta, subset)
        print(f"Analysing {len(subset)} sequences with CRW")
        ctx.invoke(
            rrna_draw,
            fasta_input=subset_fasta,
            output_folder=crw_output,
            constraint=constraint,
            exclusion=exclusion,
            fold_type=fold_type,
            skip_ribovore_filters=skip_ribovore_filters,
        )

    # RiboVision LSU
    hits = hits.union(get_hits(crw_output))
    subset = all_seq_ids.difference(hits)
    if subset:
        get_subset_fasta(fasta_input, subset_fasta, subset)
        print(f"Analysing {len(subset)} sequences with RiboVision LSU")
        ctx.invoke(
            ribovision_draw_lsu,
            fasta_input=subset_fasta,
            output_folder=ribovision_lsu_output,
            constraint=constraint,
            exclusion=exclusion,
            fold_type=fold_type,
            skip_ribovore_filters=skip_ribovore_filters,
        )

    # RNAse P
    hits = hits.union(get_hits(ribovision_lsu_output))
    subset = all_seq_ids.difference(hits)
    if subset:
        get_subset_fasta(fasta_input, subset_fasta, subset)
        print(f"Analysing {len(subset)} sequences with RNAse P models")
        ctx.invoke(
            rnasep_draw,
            fasta_input=subset_fasta,
            output_folder=rnasep_output,
            constraint=constraint,
            exclusion=exclusion,
            fold_type=fold_type,
            skip_ribovore_filters=skip_ribovore_filters,
        )

    # GtRNAdb
    hits = hits.union(get_hits(rnasep_output))
    subset = all_seq_ids.difference(hits)
    if subset:
        get_subset_fasta(fasta_input, subset_fasta, subset)
        print(f"Analysing {len(subset)} sequences with GtRNAdb")
        for trna in gtrnadb.classify_trna_sequences(subset_fasta, gtrnadb_output):
            core.visualise(
                "gtrnadb",
                fasta_input,
                output_folder + "/gtrnadb",
                trna["id"],
                None,
                constraint,
                exclusion,
                fold_type,
                trna["domain"],
                trna["isotype"],
                trna["start"],
                trna["end"],
            )

    # Rfam tRNA
    hits = hits.union(get_hits(gtrnadb_output))
    subset = all_seq_ids.difference(hits)
    if subset:
        get_subset_fasta(fasta_input, subset_fasta, subset)
        print(f"Analysing {len(subset)} sequences with Rfam tRNA")
        trna_ids = rfam.cmsearch_nohmm_mode(subset_fasta, output_folder, "RF00005")
        if trna_ids:
            get_subset_fasta(fasta_input, subset_fasta, trna_ids)
            rfam.generate_2d(
                "RF00005",
                output_folder,
                subset_fasta,
                constraint,
                exclusion,
                fold_type,
            )

    # move svg files to the final location
    result_folders = [
        crw_output,
        ribovision_ssu_output,
        ribovision_lsu_output,
        rfam_output,
        gtrnadb_output,
        rfam_trna_output,
        rnasep_output,
    ]
    for folder in result_folders:
        organise_results(folder, output_folder)
    organise_metadata(output_folder, result_folders)

    # clean up
    os.system(f"rm {output_folder}/subset*")


def organise_results(results_folder, output_folder):
    """Move files to the final folder structure."""
    destination = os.path.join(output_folder, "results")
    svg_folder = os.path.join(destination, "svg")
    thumbnail_folder = os.path.join(destination, "thumbnail")
    fasta_folder = os.path.join(destination, "fasta")
    json_folder = os.path.join(destination, "json")
    for folder in [
        destination,
        svg_folder,
        thumbnail_folder,
        fasta_folder,
        json_folder,
    ]:
        os.system(f"mkdir -p {folder}")

    svgs = glob.glob(os.path.join(results_folder, "*.colored.svg"))
    if svgs:
        for svg in svgs:
            with open(svg, "r", encoding="utf-8") as f_svg:
                thumbnail = generate_thumbnail(f_svg.read(), svg)
                thumbnail_filename = svg.replace(".colored.", ".thumbnail.")
                with open(thumbnail_filename, "w", encoding="utf-8") as f_thumbnail:
                    f_thumbnail.write(thumbnail)
        os.system(f"mv {results_folder}/*.colored.svg {svg_folder}")
        os.system(f"mv {results_folder}/*.thumbnail.svg {thumbnail_folder}")
        os.system(f"mv {results_folder}/*.fasta {fasta_folder}")
        os.system(f"mv {results_folder}/*.json {json_folder}")


@cli.group("gtrnadb")
def gtrnadb_group():
    """
    Use tRNA templates for structure visualisation.
    """


@gtrnadb_group.command("setup")
def gtrnadb_setup():
    """
    This will copy all the CM files into place so that drawing will not modify
    the data directory.
    """
    print(shared.get_r2dt_version_header())
    gtrnadb.setup()


@gtrnadb_group.command("draw")
@click.option(
    "--domain",
    default=False,
    type=click.STRING,
    help="Domain (A for Archaea, B for Bacteria, or E for Eukaryotes)",
)
@click.option(
    "--isotype", default=False, type=click.STRING, help="tRNA isotype, for example Thr"
)
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
def gtrnadb_draw(
    fasta_input,
    output_folder,
    domain="",
    isotype="",
    constraint=None,
    exclusion=None,
    fold_type=None,
):
    """
    Visualise sequences using GtRNAdb templates.
    """
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    os.system(f"mkdir -p {output_folder}")

    if domain and isotype:
        gtrnadb.visualise(
            domain.upper(),
            isotype.capitalize(),
            fasta_input,
            output_folder,
            constraint,
            exclusion,
            fold_type,
        )
    else:
        for trna in gtrnadb.classify_trna_sequences(fasta_input, output_folder):
            core.visualise(
                "gtrnadb",
                fasta_input,
                output_folder,
                trna["id"],
                None,
                constraint,
                exclusion,
                fold_type,
                trna["domain"],
                trna["isotype"],
                trna["start"],
                trna["end"],
            )


@cli.group("rnasep")
def rnasep_group():
    """
    Use RNAse P templates for structure visualisation.
    """


@rnasep_group.command("draw")
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.option(
    "--skip_ribovore_filters",
    default=False,
    is_flag=True,
    help="Ignore ribovore QC checks",
)
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
def rnasep_draw(
    fasta_input, output_folder, constraint, exclusion, fold_type, skip_ribovore_filters
):
    """Draw 2D diagrams using RNAse P templates."""
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    os.system(f"mkdir -p {output_folder}")
    with open(
        get_ribotyper_output(
            fasta_input, output_folder, config.RNASEP_CM_LIBRARY, skip_ribovore_filters
        ),
        "r",
        encoding="utf-8",
    ) as f_ribotyper:
        for line in f_ribotyper.readlines():
            rnacentral_id, model_id, _ = line.split("\t")
            core.visualise(
                "rnasep",
                fasta_input,
                output_folder,
                rnacentral_id,
                model_id,
                constraint,
                exclusion,
                fold_type,
            )


@cli.group("crw")
def crw_group():
    """
    Use CRW templates for structure visualisation.
    """


@crw_group.command("draw")
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.option(
    "--skip_ribovore_filters",
    default=False,
    is_flag=True,
    help="Ignore ribovore QC checks",
)
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
def rrna_draw(
    fasta_input, output_folder, constraint, exclusion, fold_type, skip_ribovore_filters
):
    """Draw 2D diagrams using CRW templates."""
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    os.system(f"mkdir -p {output_folder}")
    with open(
        get_ribotyper_output(
            fasta_input, output_folder, config.CRW_CM_LIBRARY, skip_ribovore_filters
        ),
        "r",
        encoding="utf-8",
    ) as f_ribotyper:
        for line in f_ribotyper.readlines():
            rnacentral_id, model_id, _ = line.split("\t")
            core.visualise(
                "crw",
                fasta_input,
                output_folder,
                rnacentral_id,
                model_id,
                constraint,
                exclusion,
                fold_type,
            )


@cli.group("ribovision")
def ribovision_group():
    """
    Use RiboVision templates for structure visualisation.
    """


@ribovision_group.command("draw_lsu")
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.option(
    "--skip_ribovore_filters",
    default=False,
    is_flag=True,
    help="Ignore ribovore QC checks",
)
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
def ribovision_draw_lsu(
    fasta_input, output_folder, constraint, exclusion, fold_type, skip_ribovore_filters
):
    """Draw 2D diagrams using LSU templates from RiboVision."""
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    os.system(f"mkdir -p {output_folder}")
    with open(
        get_ribotyper_output(
            fasta_input,
            output_folder,
            config.RIBOVISION_LSU_CM_LIBRARY,
            skip_ribovore_filters,
        ),
        "r",
        encoding="utf-8",
    ) as f_ribotyper:
        for line in f_ribotyper.readlines():
            rnacentral_id, model_id, _ = line.split("\t")
            core.visualise(
                "lsu",
                fasta_input,
                output_folder,
                rnacentral_id,
                model_id,
                constraint,
                exclusion,
                fold_type,
            )


@ribovision_group.command("draw_ssu")
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.option(
    "--skip_ribovore_filters",
    default=False,
    is_flag=True,
    help="Ignore ribovore QC checks",
)
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
def ribovision_draw_ssu(
    fasta_input, output_folder, constraint, exclusion, fold_type, skip_ribovore_filters
):
    """Draw 2D diagrams using SSU templates from RiboVision."""
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    os.system(f"mkdir -p {output_folder}")
    with open(
        get_ribotyper_output(
            fasta_input,
            output_folder,
            config.RIBOVISION_SSU_CM_LIBRARY,
            skip_ribovore_filters,
        ),
        "r",
        encoding="utf-8",
    ) as f_ribotyper:
        for line in f_ribotyper.readlines():
            rnacentral_id, model_id, _ = line.split("\t")
            core.visualise(
                "ssu",
                fasta_input,
                output_folder,
                rnacentral_id,
                model_id,
                constraint,
                exclusion,
                fold_type,
            )


@cli.group("rfam")
def rfam_group():
    """
    Use Rfam templates for structure visualisation.
    """


@rfam_group.command("blacklisted")
def rfam_blacklist():
    """
    Show all blacklisted families. These include rRNA families as well as
    families that do not have any secondary structure.
    """
    for model in sorted(rfam.blacklisted()):
        print(model)


@rfam_group.command("draw")
@click.option(
    "--constraint", default=False, is_flag=True, help="Fold insertions using RNAfold"
)
@click.option("--exclusion", default=None)
@click.option("--fold_type", default=None)
@click.argument("rfam_acc", type=click.STRING)
@click.argument("fasta-input", type=click.Path())
@click.argument("output-folder", type=click.Path())
def rfam_draw(
    rfam_acc,
    fasta_input,
    output_folder,
    constraint=None,
    exclusion=None,
    fold_type=None,
):
    """
    Visualise sequences using the Rfam/R-scape consensus structure as template.

    RFAM_ACCESSION - Rfam family to process (RF00001, RF00002 etc)
    """
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    print(rfam_acc)
    if rfam.has_structure(rfam_acc):
        rfam.generate_2d(
            rfam_acc,
            output_folder,
            fasta_input,
            constraint,
            exclusion,
            fold_type,
        )
    else:
        print(f"{rfam_acc} does not have a conserved secondary structure")


@rfam_group.command("validate")
@click.argument("rfam_accession", type=click.STRING)
@click.argument("output", type=click.File("w"))
def rfam_validate(rfam_accession, output):
    """
    print("Validating")
    Check if the given Rfam accession is one that should be drawn. If so it will
    be output to the given file, otherwise it will not.
    """
    if rfam_accession not in rfam.blacklisted():
        output.write(f"{rfam_accession}\n")


def generate_thumbnail(image, description):
    """Generate a thumbnail SVG as an outline of the 2D diagram."""
    move_to_start_position = None
    color = ColorHash(description).hex
    points = []
    for _, line in enumerate(image.split("\n")):
        if "width" in line and not "stroke-width" in line:
            width = re.findall(r'width="(\d+(\.\d+)?)"', line)
        if "height" in line:
            height = re.findall(r'height="(\d+(\.\d+)?)"', line)
        for nt_block in re.finditer(
            r'<text x="(\d+)(\.\d+)?" y="(\d+)(\.\d+)?".*?</text>', line
        ):
            if "numbering-label" in nt_block.group(0):
                continue
            if not move_to_start_position:
                move_to_start_position = f"M{nt_block.group(1)} {nt_block.group(3)} "
            points.append(f"L{nt_block.group(1)} {nt_block.group(3)}")
    if len(points) < 200:
        stroke_width = "3"
    elif len(points) < 500:
        stroke_width = "4"
    elif len(points) < 3000:
        stroke_width = "4"
    else:
        stroke_width = "2"
    thumbnail = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width[0][0]}" height="{height[0][0]}">'
        f'<path style="stroke:{color};stroke-width:{stroke_width}px;'
        f'fill:none;" d="'
    )
    thumbnail += move_to_start_position
    thumbnail += " ".join(points)
    thumbnail += '"/></svg>'
    return thumbnail


def organise_metadata(output_folder, result_folders):
    """
    Aggregate hits.txt files from all subfolders.
    """
    tsv_folder = os.path.join(output_folder, "results", "tsv")
    os.system(f"mkdir -p {tsv_folder}")
    with open(os.path.join(tsv_folder, "metadata.tsv"), "w", encoding="utf-8") as f_out:
        for folder in result_folders:
            hits = os.path.join(folder, "hits.txt")
            if not os.path.exists(hits):
                continue
            with open(hits, "r", encoding="utf-8") as f_hits:
                for line in f_hits.readlines():
                    if "gtrnadb" in folder:
                        line = line.replace("PASS", "GtRNAdb")
                    elif "crw" in folder:
                        line = line.replace("PASS", "CRW").replace("FAIL", "CRW")
                    elif "rfam" in folder or "RF00005" in folder:
                        line = line.replace("PASS", "Rfam").replace("FAIL", "Rfam")
                    elif "ribovision-lsu" in folder or "ribovision-ssu" in folder:
                        line = line.replace("PASS", "RiboVision").replace(
                            "FAIL", "RiboVision"
                        )
                    elif "rnasep" in folder:
                        line = line.replace("PASS", "RNAse P Database").replace(
                            "FAIL", "RNAse P Database"
                        )
                    f_out.write(line)


@cli.command()
@click.argument("cm_library", type=click.Path())
def generatemodelinfo(cm_library):
    """
    Helper for generating modelinfo.txt files.
    """
    print(shared.get_r2dt_version_header())
    gmi.generate_model_info(cm_library)


def force_draw(
    model_id,
    fasta_input,
    output_folder,
    seq_id,
    constraint=None,
    exclusion=None,
    fold_type=None,
):
    """Draw 2D diagrams using a specified template."""
    # pylint: disable=too-many-arguments
    print(shared.get_r2dt_version_header())
    model_type = lm.get_model_type(model_id)
    if not model_type:
        print("Error: Model not found. Please check model_id")
        return
    print(f"Visualising sequence {seq_id} using the {model_id} model from {model_type}")
    os.system(f"esl-sfetch --index {fasta_input}")

    output = os.path.join(output_folder, model_type.replace("_", "-"))

    if model_type == "rfam":
        core.visualise(
            "rfam",
            fasta_input,
            output,
            seq_id,
            model_id,
            constraint,
            exclusion,
            fold_type,
        )
    elif model_type == "ribovision_ssu":
        core.visualise(
            "ssu",
            fasta_input,
            output,
            seq_id,
            model_id,
            constraint,
            exclusion,
            fold_type,
        )
    elif model_type == "ribovision_lsu":
        core.visualise(
            "lsu",
            fasta_input,
            output,
            seq_id,
            model_id,
            constraint,
            exclusion,
            fold_type,
        )
    elif model_type == "rnasep":
        core.visualise(
            "rnasep",
            fasta_input,
            output,
            seq_id,
            model_id,
            constraint,
            exclusion,
            fold_type,
        )
    elif model_type == "crw":
        core.visualise(
            "crw",
            fasta_input,
            output,
            seq_id,
            model_id,
            constraint,
            exclusion,
            fold_type,
        )
    elif model_type == "gtrnadb":
        domain, isotype = model_id.split("_")
        gtrnadb.visualise(
            domain, isotype, fasta_input, output, constraint, exclusion, fold_type
        )
    # organise results into folders
    organise_results(output, output_folder)
    metadata_folder = os.path.join(output_folder, "results", "tsv")
    if not os.path.exists(metadata_folder):
        os.makedirs(metadata_folder)
    label_mapping = {
        "crw": "CRW",
        "gtrnadb": "GtRNAdb",
        "rfam": "Rfam",
        "ribovision_ssu": "RiboVision",
        "ribovision_lsu": "RiboVision",
        "rnasep": "RNAse P database",
    }
    with open(
        os.path.join(metadata_folder, "metadata.tsv"), "a", encoding="utf-8"
    ) as f_out:
        line = f"{seq_id}\t{model_id}\t{label_mapping[model_type]}\n"
        f_out.write(line)


@cli.command()
def list_models():
    """
    List all installed templates.
    """
    print(shared.get_r2dt_version_header())
    data = lm.list_models()
    for item in data:
        print(item["description"])
    lm.check_unique_descriptions(data)
    with open(
        os.path.join(config.DATA, "models.json"), "w", encoding="utf-8"
    ) as models_file:
        json.dump(data, models_file)


@cli.command()
@click.argument("test_name", required=False, default=None, type=click.STRING)
def test(test_name):
    """
    Run all tests or a special test if provided.
    """
    if test_name:
        cmd = f"R2DT_KEEP_TEST_RESULTS=1 python3 -m unittest tests.tests.{test_name}"
        print(cmd)
        os.system(cmd)
    else:
        os.system("R2DT_KEEP_TEST_RESULTS=1 python3 -m unittest")


@cli.command()
@click.argument("test_name", required=True, type=click.STRING)
def update_test_examples(test_name):
    """Update test examples for a given test."""
    try:
        class_ = getattr(tests, test_name)
    except AttributeError:
        print(f"Error: {test_name} is not found in tests.py")
        return
    test_instance = class_()
    for example_file in test_instance.files:
        print(example_file)
        old_filename = os.path.join(
            test_instance.test_results,
            test_instance.test_results_subfolder,
            example_file,
        )
        new_filename = os.path.join(test_instance.precomputed_results, example_file)
        shutil.copyfile(old_filename, new_filename)


@cli.command()
def generatecm():
    """
    Helper for generating covariance models.
    """
    print(shared.get_r2dt_version_header())
    for bpseq in glob.glob(f"{config.BPSEQ_LOCATION}/*.bpseq"):
        fasta = gcl.convert_bpseq_to_fasta(bpseq)
    for fasta in glob.glob(f"{config.BPSEQ_LOCATION}/*.fasta"):
        print(os.path.basename(fasta).replace(".fasta", ""))
        # fasta_no_knots = break_pseudoknots(fasta)
        stockholm = gcl.convert_fasta_to_stockholm(fasta)
        gcl.build_cm(stockholm, config.BPSEQ_LOCATION)
    print("Done")


if __name__ == "__main__":
    cli()

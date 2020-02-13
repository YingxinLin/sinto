import pysam
from sinto import utils
from scipy import sparse
import numpy as np
from collections import Counter, defaultdict
from multiprocessing import Pool
import functools


def writeFragments(fragments, filepath):
    """Write fragments to file

    Parameters
    ----------
    fragments : list
        List of ATAC fragments
    filepath : str
        Path for output file
    """
    with open(filepath, "w") as outf:
        for i in fragments:
            outstr = "\t".join(map(str, i))
            outf.write(outstr + "\n")


def collapseFragments(fragments):
    """Collapse duplicate fragments
    """
    fraglist = [list(x.values()) for x in list(fragments.values())]
    fragcoords_with_bc = ["|".join(map(str, x)) for x in fraglist]
    fragcoords = ["|".join(map(str, x[:3])) for x in fraglist]
    cellbarcodes = [x[3] for x in fraglist]
    counts = Counter(fragcoords_with_bc)
    # enumerate fragments and barcodes
    frag_id_lookup = id_lookup(l=fragcoords)
    bc_id_lookup = id_lookup(l=cellbarcodes)

    # get list of barcode index and fragment index from counts
    row = []
    col = []
    data = []
    for i in list(counts.items()):
        data.append(i[1])
        rowstr = i[0].split("|")[:3]
        bcstr = i[0].split("|")[3]
        row.append(frag_id_lookup["|".join(rowstr)])
        col.append(bc_id_lookup[bcstr])

    # create sparse matrix of fragment counts from fraglist (column, row, value)
    mat = sparse.coo_matrix(
        (data, (np.array(row), np.array(col))),
        shape=(max(frag_id_lookup.values()) + 1, max(bc_id_lookup.values()) + 1),
    )
    mat = mat.tocsr()

    # find which barcode contains the most counts for each fragment
    rowsums = mat.sum(axis=1)
    colsums = mat.sum(axis=0)
    rowmax = np.argmax(mat, axis=1)
    rowsum = mat.sum(axis=1).tolist()

    # collapse back into a list of fragment coords and barcodes
    frag_inverse = dict(zip(frag_id_lookup.values(), frag_id_lookup.keys()))
    bc_inverse = dict(zip(bc_id_lookup.values(), bc_id_lookup.keys()))
    collapsed_frags = [frag_inverse[x] for x in np.arange(mat.shape[0])]
    collapsed_barcodes = [bc_inverse[x[0]] for x in rowmax.tolist()]
    collapsed = []
    for i in range(len(collapsed_barcodes)):
        frag = collapsed_frags[i].split("|")
        frag.append(collapsed_barcodes[i])
        frag.append(rowsum[i][0])
        collapsed.append(frag)
    return collapsed


def id_lookup(l):
    """Create dictionary where each unique item is key, value is the item numerical ID"""
    temp = defaultdict(lambda: len(temp))
    idx = [temp[x] for x in l]
    lookup = dict(zip(set(l), set(idx)))
    return lookup


def getFragments(
    interval, bam, min_mapq=30, cellbarcode="CB", readname_barcode=None, cells=None
):
    """Extract ATAC fragments from BAM file

    Iterate over paired reads in a BAM file and extract the ATAC fragment coordinates

    Parameters
    ----------
    bam : str
        Path to BAM file
    min_mapq : int
        Minimum MAPQ to retain fragment
    cellbarcode : str
        Tag used for cell barcode. Default is CB (used by cellranger)
    readname_barcode : str, optional
        Regex to extract cell barcode from readname. If None,
        use the read tag instead.
    cells : list, optional
        List of cell barocodes to retain
    """
    fragment_dict = dict()
    inputBam = pysam.AlignmentFile(bam, "rb")
    if readname_barcode is not None:
        readname_barcode = re.compile(readname_barcode)
    for i in inputBam.fetch(interval[0], 0, interval[1]):
        fragment_dict = updateFragmentDict(
            fragments=fragment_dict,
            segment=i,
            min_mapq=min_mapq,
            cellbarcode=cellbarcode,
            readname_barcode=readname_barcode,
            cells=cells,
        )
    fragment_dict = filterFragmentDict(fragments=fragment_dict)
    collapsed = collapseFragments(fragments=fragment_dict)
    return collapsed


def updateFragmentDict(
    fragments, segment, min_mapq, cellbarcode, readname_barcode, cells
):
    """Update dictionary of ATAC fragments
    Takes a new aligned segment and adds information to the dictionary,
    returns a modified version of the dictionary

    Positions are 0-based
    Reads aligned to the + strand are shifted +4 bp
    Reads aligned to the - strand are shifted -5 bp

    Parameters
    ----------
    fragments : dict
        A dictionary containing ATAC fragment information
    segment : pysam.AlignedSegment
        An aligned segment
    min_mapq : int
        Minimum MAPQ to retain fragment
    cellbarcode : str
       Tag used for cell barcode. Default is CB (used by cellranger)
    readname_barcode : regex
        A compiled regex for matching cell barcode in read name. If None,
        use the read tags.
    cells : list
        List of cells to retain. If None, retain all cells found.
    """
    # because the cell barcode is not stored with each read pair (only one of the pair)
    # we need to look for each read separately rather than using the mate cigar / mate postion information
    if readname_barcode is not None:
        re_match = readname_barcode.match(segment.qname)
        cell_barcode = re_match.group()
    else:
        cell_barcode, _ = utils.scan_tags(segment.tags, cb=cellbarcode)
    if cells is not None and cell_barcode is not None:
        if cell_barcode not in cells:
            return fragments
    mapq = segment.mapping_quality
    if mapq < min_mapq:
        return fragments
    chromosome = segment.reference_name
    qname = segment.query_name
    rstart = segment.reference_start
    rend = segment.reference_end
    qstart = segment.query_alignment_start
    is_reverse = segment.is_reverse
    if rend is None:
        return fragments
    # correct for soft clipping
    rstart = rstart + qstart
    # correct for 9 bp Tn5 shift
    if is_reverse:
        rend = rend - 5
    else:
        rstart = rstart + 4
    if qname in fragments.keys():
        if is_reverse:
            fragments[qname]["end"] = rend
        else:
            fragments[qname]["start"] = rstart
    else:
        fragments[qname] = {
            "chrom": chromosome,
            "start": None,
            "end": None,
            "cell": cell_barcode,
        }
        if is_reverse:
            fragments[qname]["end"] = rend
        else:
            fragments[qname]["start"] = rstart
    return fragments


def filterFragmentDict(fragments):
    """Remove invalid entries"""
    allkey = list(fragments.keys())
    for key in allkey:
        if not all(fragments[key].values()):
            del fragments[key]
    return fragments


def condenseFragList(fraglist):
    """Condense multiple lists of fragments into one
    Input should be a 3-deep list
    """
    x = []
    for i in fraglist:
        for j in i:
            for y in j:
                x.append(y)
    return x


def fragments(
    bam,
    fragment_path,
    min_mapq=30,
    nproc=1,
    cellbarcode="CB",
    chromosomes="(?i)^chr",
    readname_barcode=None,
    cells=None,
):
    """Create ATAC fragment file from BAM file

    Iterate over reads in BAM file, extract fragment coordinates and cell barcodes.
    Collapse sequencing duplicates.

    Parameters
    ----------
    bam : str
        Path to BAM file
    fragment_path : str
        Path for output fragment file
    min_mapq : int
        Minimum MAPQ to retain fragment
    nproc : int, optional
        Number of processors to use. Default is 1.
    cellbarcode : str
       Tag used for cell barcode. Default is CB (used by cellranger)
    chromosomes : str, optional
        Regular expression used to match chromosome names to include in the
        output file. Default is "(?i)^chr" (starts with "chr", case-insensitive).
        If None, use all chromosomes in the BAM file.
    readname_barcode : str, optional
        Regular expression used to match cell barocde stored in read name. 
        If None (default), use read tags instead. Use "[^:]*" to match all characters 
        before the first colon (":").
    cells : str
        File containing list of cell barcodes to retain. If None (default), use all cell barcodes
        found in the BAM file.
    """
    nproc = int(nproc)
    chrom = utils.get_chromosomes(bam, keep_contigs=chromosomes)
    cells = utils.read_cells(cells)
    p = Pool(nproc)
    frag_lists = [
        p.map_async(
            functools.partial(
                getFragments,
                bam=bam,
                min_mapq=int(min_mapq),
                cellbarcode=cellbarcode,
                readname_barcode=readname_barcode,
                cells=cells,
            ),
            list(chrom.items()),
        )
    ]
    frags = condenseFragList([res.get() for res in frag_lists])
    writeFragments(fragments=frags, filepath=fragment_path)

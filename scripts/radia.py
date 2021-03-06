#!/usr/bin/env python

import sys
import time
import re
import os
import subprocess
import datetime
import logging
from optparse import OptionParser
from itertools import izip
import radiaUtil
import collections


'''
'    RNA and DNA Integrated Analysis (RADIA):
'    Identifies RNA and DNA variants in NGS data.
'
'    Copyright (C) 2010  Amie J. Radenbaugh, Ph.D.
'
'    This program is free software: you can redistribute it and/or modify
'    it under the terms of the GNU Affero General Public License as
'    published by the Free Software Foundation, either version 3 of the
'    License, or (at your option) any later version.
'
'    This program is distributed in the hope that it will be useful,
'    but WITHOUT ANY WARRANTY; without even the implied warranty of
'    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
'    GNU Affero General Public License for more details.
'
'    You should have received a copy of the GNU Affero General Public License
'    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'
'    This program identifies RNA and DNA variants in BAM files.  The program
'    is designed to take in 4 BAM files:  DNA Normal, RNA Normal, DNA Tumor,
'    and RNA Tumor.  For the normal DNA, the program outputs any differences
'    compared to the reference which could be potential Germline mutations.
'    For the normal RNA, the program outputs any differences compared to the
'    reference and the normal DNA which could be potential normal RNA-Editing
'    events.  For the tumor DNA, the program outputs any difference compared
'    to the reference, normal DNA and normal RNA which could be potential
'    Somatic mutations.  For the tumor RNA, the program outputs any difference
'    compared to the reference, normal DNA, normal RNA and tumor DNA which
'    could be potential RNA-editing events.
'
'    The program is designed for 4 BAM files, but the user can also specify
'    just two or three. The program will report RNA and DNA variants.
'
'''

# this regular expression is used to remove
# insertions and deletions from raw reads
# a read could look like:  "T$TT+3AGGGT+2AG+2AG.-2AGGG..-1A"
# insertions start with a "+", deletions with a "-"
# in theory, there could be multiple digits
i_numOfIndelsRegEx = re.compile("[+-]{1}(\\d+)")

# this regular expression will match any number of valid cDNA strings
i_cDNARegEx = re.compile("[ACGTNacgtn]+")

# this regular expression will match full TCGA sample Ids,
# e.g. TCGA-AG-A016-01A-01R or TCGA-37-4133-10A-01D
i_tcgaNameRegEx = re.compile("TCGA-(\\w){2}-(\\w){4}-(\\w){3}-(\\w){3}")


def get_chrom_size(aChrom, anInputStream, anIsDebug):
    '''
    ' This function reads from a FASTA index file.
    ' The FASTA index file has 5 columns:
    ' NAME        Name of this reference sequence
    ' LENGTH      Total length of this reference sequence, in bases
    ' OFFSET      Offset within the FASTA file of this sequence's first base
    ' LINEBASES   The number of bases on each line
    ' LINEWIDTH   The number of bytes in each line, including the newline
    '
    ' Here is an example:
    ' 1       249250621       3       50      51
    ' 2       243199373       254235640       50      51
    ' 3       198022430       502299004       50      51
    ' 4       191154276       704281886       50      51
    ' ...       ...
    '
    ' aChrom: The chrom size to return
    ' anInputStream: The input stream for the FASTA index file
    '''

    for line in anInputStream:

        # if it is an empty line or header line, then just continue
        if (line.isspace() or line.startswith("#")):
            continue

        # strip the carriage return and newline characters
        line = line.rstrip("\r\n")

        # split the line on the tab
        splitLine = line.split("\t")

        # the coordinate is the second element
        chrom = splitLine[0]
        size = int(splitLine[1])

        # sometimes the chroms have the "chr" prefix, sometimes they don't
        if (chrom == aChrom or chrom == "chr" + str(aChrom)):
            if (anIsDebug):
                logging.debug("get_chrom_size(): found size of " +
                              "chrom %s, size=%s", aChrom, size)
            return size

    return -1


def get_batch_end_coordinate(aStartCoordinate, anEndCoordinate, aBatchSize):
    '''
    ' This function takes a start coordinate, an end coordinate, and a batch
    ' size and returns the next appropriate batch end coordinate which is
    ' either the start coordinate plus the batch size if this is less than
    ' the final end coordinate otherwise the end coordinate.
    '
    ' aStartCoordinate:  A start coordinate
    ' anEndCoordinate:  A stop coordinate
    ' aBatchSize:  A batch size
    '''
    if ((aStartCoordinate + aBatchSize) <= anEndCoordinate):
        # we don't want to have the end coordinate be the same as the next
        # batch's start coordinate, so make sure to do a minus one here
        return (aStartCoordinate + aBatchSize - 1)
    else:
        return (anEndCoordinate)


def get_sam_data(aSamFile, aChrom, aStartCoordinate,
                 aStopCoordinate, aSourcePrefix, anIsDebug):
    '''
    ' This function uses the python generator to yield the information for one
    ' coordinate at a time. This function is used during testing to read data
    ' from a .sam input file and can also be used when the user specifies an
    ' mpileup file instead of a bam file as input.  This function yields the
    ' chromosome, coordinate, reference base, number of reads, raw reads, and
    ' quality scores.
    '
    ' aSamFile:              A .sam file or .mpileups file
    ' aChrom:                The chromosome
    ' aStartCoordinate:      The initial start coordinate
    ' aStopCoordinate:       The initial stop coordinate (size of the chrom)
    ' aSourcePrefix:         A label for the input file used when debugging
    '''

    # open the sam file
    samFileHandler = radiaUtil.get_read_fileHandler(aSamFile)

    for line in samFileHandler:

        # if the samtools select statement returns no reads which can happen
        # when the batch size is small and the selection is done in an area
        # with no reads, then a warning message will be returned that starts
        # with "[mpileup]".  We can ignore the message and move on to the next
        # select statement.
        if (line.isspace() or line.startswith("[mpileup]")):
            continue

        # strip the carriage return and newline characters
        line = line.rstrip("\r\n")

        if (anIsDebug):
            logging.debug("Original SAM pileup on %s: %s", aSourcePrefix, line)

        # split the .sam line on the tab
        splitLine = line.split("\t")

        if (len(splitLine) > 1):
            # make sure the chrom is the same
            chrom = splitLine[0]
            if (chrom != aChrom):
                continue

            # the user can specify a pileups file and only be interested in
            # one coordinate, so let them specify that one coordinate with
            # the -a and -z params and be done
            coordinate = int(splitLine[1])
            if (coordinate < aStartCoordinate):
                continue
            if (coordinate > aStopCoordinate):
                break
            reference = splitLine[2].upper()
            numOfReads = int(splitLine[3])
            reads = splitLine[4]
            baseQuals = splitLine[5]
            mapQuals = splitLine[6]
        else:
            continue

        # yield all the information about the current coordinate
        yield (chrom, coordinate, reference, numOfReads,
               reads, baseQuals, mapQuals)

    samFileHandler.close()
    return


def get_bam_data(aBamFile, aFastaFile, aChrom, aStartCoordinate,
                 aStopCoordinate, aBatchSize, aUseChrPrefix, aSourcePrefix,
                 anRnaIncludeSecondaryAlignmentsFlag, anIsDebug):
    '''
    ' This function uses the python generator to yield the information for one
    ' coordinate at a time. In order to reduce the time and memory overhead of
    ' loading the entire .bam file into memory at once, this function reads in
    ' chunks of data at a time.  The number of coordinates that should be read
    ' into memory at a given time is determined by the "aBatchSize" parameter.
    ' This function uses the samtools "mpileup" command to make a selection.
    '
    ' The original start and end coordinates are specified by the
    ' "aStartCoordinate" and "anEndCoordinate" parameters and are typically
    ' initialized to 0 and the size of the chromosome respectively. This
    ' function will loop over the .bam file, selecting "aBatchSize" number
    ' of coordinates into memory at once.  Each line that is selected will be
    ' processed and yielded using the python generator.  When all lines from
    ' the current batch are processed, the start and end coordinates will be
    ' incremented, and the next selection will be made from the .bam file.
    ' This process continues until the end of the chromosome has been reached.
    '
    ' This function yields the chromosome, coordinate, reference base,
    ' number of reads, raw reads, and the quality scores.
    '
    ' aBamFile:
    '    A .bam file to be read from
    ' aFastaFile:
    '    The FASTA file that should be used in the samtools
    '    command which is needed for the reference base.
    ' aChrom:
    '    The chromosome that should be used in the samtools command
    ' aStartCoordinate:
    '    The initial start coordinate (typically zero)
    ' aStopCoordinate:
    '    The initial stop coordinate (typically the size of the chromosome)
    ' aBatchSize:
    '    The number of coordinates to load into memory at one time
    ' aUseChrPrefix:
    '    Whether the 'chr' should be used in the region
    '    parameter of the samtools command
    ' aSourcePrefix:
    '    A label used when debugging to determine the input file
    ' anRnaIncludeSecondayAlignmentsFlag:
    '    If you align the RNA to transcript isoforms, then you may want
    '    to include RNA secondary alignments in the samtools mpileups
    '''

    # initialize the first start and stop coordinates
    # the stop coordinate is calculated according to the "aBatchSize" param
    currentStartCoordinate = aStartCoordinate
    currentStopCoordinate = get_batch_end_coordinate(currentStartCoordinate,
                                                     aStopCoordinate,
                                                     aBatchSize)

    # while we still have coordinates to select from the .bam file
    while (currentStartCoordinate <= aStopCoordinate):

        # execute the samtools command
        pileups = execute_samtools_cmd(aBamFile,
                                       aFastaFile,
                                       aChrom,
                                       aUseChrPrefix,
                                       currentStartCoordinate,
                                       currentStopCoordinate,
                                       anRnaIncludeSecondaryAlignmentsFlag,
                                       anIsDebug)

        numPileups = 0

        # for each line representing one coordinate
        for line in pileups:

            # if the samtools select statement returns no reads which can
            # happen when the batch size is small and the selection is done
            # in an area with no reads, then a warning message will be
            # returned that starts with "[mpileup]".  We can ignore the
            # message and move on to the next select statement.
            if (line.isspace() or
                line.startswith("[mpileup]") or
                line.startswith("<mpileup>")):
                continue

            # strip the carriage return and newline characters
            line = line.rstrip("\r\n")

            # split the line on the tab
            splitLine = line.split("\t")

            if (anIsDebug):
                logging.debug("Original BAM pileup for %s: %s",
                              aSourcePrefix, line)

            if (len(splitLine) > 1):
                # the coordinate is the second element
                chrom = splitLine[0]
                coordinate = int(splitLine[1])
                reference = splitLine[2].upper()
                numOfReads = int(splitLine[3])
                reads = splitLine[4]
                baseQualScores = splitLine[5]
                mapQualScores = splitLine[6]
            else:
                continue

            # yield all the information about the current coordinate
            yield (chrom, coordinate, reference, numOfReads,
                   reads, baseQualScores, mapQualScores)
            numPileups += 1

        if (anIsDebug):
            logging.debug("samtools number of lines from %s to %s = %s",
                          currentStartCoordinate,
                          currentStopCoordinate,
                          numPileups)

        # calculate a new start and stop coordinate
        # for the next select statement
        currentStartCoordinate = currentStartCoordinate + aBatchSize
        currentStopCoordinate = get_batch_end_coordinate(
                                                    currentStartCoordinate,
                                                    aStopCoordinate,
                                                    aBatchSize)

    return


def execute_samtools_cmd(aBamFile, aFastaFile, aChrom, aUseChrPrefix,
                         aStartCoordinate, aStopCoordinate,
                         anRnaIncludeSecondaryAlignmentsFlag, anIsDebug):
    '''
    ' This function executes an external command.  The command is the
    ' "samtools mpileup" command which returns all the information about the
    ' sequencing reads for specific coordinates.  There are two things to be
    ' careful about when using the samtools mpileup command.  Some .bam files
    ' use the 'chr' prefix when specifying the region to select with the -r
    ' argument.  If the 'chr' prefix is required, then specify the
    ' --useChrPrefix argument and also make sure that the fasta file that
    ' is specified also has the 'chr' prefix.  Here are some examples of
    ' the commands that can be used to view the output:
    '
    ' samtools mpileup -f /path/to/fasta/hg19.fa -Q 20 -q 10
    '    -r chr1:855155-1009900 /path/to/bams/myBam.bam
    ' samtools mpileup -f /path/to/fasta/hg19.fa -Q 20 -q 10
    '    -r 1:855155-1009900 /path/to/bams/myBam.bam
    '
    ' aBamFile:
    '    A .bam file to be read from
    ' aFastaFile:
    '    The FASTA file which is needed for the reference base.
    ' aChrom:
    '    The chromosome that we are selecting from
    ' aUseChrPrefix:
    '    Whether the 'chr' should be used in the samtools command
    ' aStartCoordinate:
    '    The start coordinate of the selection
    ' aStopCoordinate:
    '    The stop coordinate of the selection
    ' anRnaIncludeSecondayAlignmentsFlag:
    '    If you align the RNA to transcript isoforms, then you may want
    '    to include RNA secondary alignments in the samtools mpileups
    '''
    # create the samtools command
    if (aUseChrPrefix):
        samtoolsCmd = "samtools mpileup -E -s"
        samtoolsCmd += " -f " + aFastaFile
        samtoolsCmd += " -Q 0 -q 0 "
        samtoolsCmd += " -r chr" + aChrom
        samtoolsCmd += ":" + str(aStartCoordinate)
        samtoolsCmd += "-" + str(aStopCoordinate)
        samtoolsCmd += " " + aBamFile
    else:
        samtoolsCmd = "samtools mpileup -E -s"
        samtoolsCmd += " -f " + aFastaFile
        samtoolsCmd += " -Q 0 -q 0"
        samtoolsCmd += " -r " + aChrom
        samtoolsCmd += ":" + str(aStartCoordinate)
        samtoolsCmd += "-" + str(aStopCoordinate)
        samtoolsCmd += " " + aBamFile

    # -ff (exclude flags):  unmapped reads, failed quality checks, pcr dups
    # -rf (include flags):  everything else (including secondary alignments)
    if (anRnaIncludeSecondaryAlignmentsFlag):
        samtoolsCmd += " --ff 1540 --rf 2555"

    # output the samtools command
    if (anIsDebug):
        logging.debug(samtoolsCmd)

    # execute the samtools command
    timeSamtoolsStart = time.time()
    samtoolsCall = subprocess.Popen(samtoolsCmd,
                                    shell=True,
                                    bufsize=4096,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    close_fds=True)
    '''
    samtoolsCall = subprocess.Popen(samtoolsCmd,
                                    shell=True,
                                    bufsize=-1,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    close_fds=True)
    '''

    for line in samtoolsCall.stdout:
        yield line

    # communicate() waits for the process to finish
    # (pileups, samtoolsStdErr) = samtoolsCall.communicate()
    samtoolsStdErr = samtoolsCall.wait()

    timeSamtoolsEnd = time.time()
    timeSpent = timeSamtoolsEnd-timeSamtoolsStart

    if (anIsDebug):
        logging.debug("samtools mpileup time: %s hrs, %s mins, %s secs",
                      (timeSpent/3600), (timeSpent/60), (timeSpent))

    # if the return code is None, then the process is not yet finished
    # communicate() waits for the process to finish, poll() does not
    if (samtoolsCall.returncode is None):
        logging.warning("The samtools mpileup command is not done, " +
                        "indicating an error.")
    # if samtools returned a return code != 0, then an error occurred
    # warning: previous versions of samtools did not return a return code!
    elif (samtoolsCall.returncode != 0):
        logging.warning("The return code of '%s' from the samtools mpileup "
                        "command indicates an error.", samtoolsCall.returncode)
        logging.warning("Warning/error from %s:\n%s",
                        samtoolsCmd, samtoolsStdErr)
    return


def convert_raw_base(aBase, aRawBaseQual, aRawMapQual, aConvertedBaseQual,
                     aConvertedMapQual, aMinBaseQual, aMinMapQual, aFinalBases,
                     aFinalBaseQuals, aFinalMapQuals, aNumBasesDict,
                     aNumPlusStrandDict, aSumBaseQualsDict, aSumMapQualsDict,
                     aSumMapQualZeroesDict, aMaxMapQualsDict, anIsPlusStrand):

    # count the number of mapping qualities that are zero per allele
    if (aConvertedMapQual == 0):
        aSumMapQualZeroesDict[aBase] += 1

    # if the quals are above the mins
    if aConvertedBaseQual >= aMinBaseQual and aConvertedMapQual >= aMinMapQual:
        aFinalBases += aBase
        aFinalBaseQuals += aRawBaseQual
        aFinalMapQuals += aRawMapQual
        aNumBasesDict[aBase] += 1
        aSumBaseQualsDict[aBase] += aConvertedBaseQual
        aSumMapQualsDict[aBase] += aConvertedMapQual

        # if this is on the plus strand
        if (anIsPlusStrand):
            aNumPlusStrandDict[aBase] += 1
        # keep track of the max mapping quality per allele
        if (aConvertedMapQual > aMaxMapQualsDict[aBase]):
            aMaxMapQualsDict[aBase] = aConvertedMapQual

    return (aFinalBases, aFinalBaseQuals, aFinalMapQuals, aNumBasesDict,
            aNumPlusStrandDict, aSumBaseQualsDict, aSumMapQualsDict,
            aSumMapQualZeroesDict, aMaxMapQualsDict)


def convert_and_filter_raw_reads(aChr, aCoordinate, aStringOfRawReads,
                                 aStringOfRawBaseQuals, aStringOfRawMapQuals,
                                 aReferenceBase, aMinBaseQual,
                                 aMinMapQual, anIsDebug):
    '''
    ' This function returns all of the valid RNA (cDNA) or DNA bases from the
    ' given pileup of read bases. It converts all of the samtools specific
    ' characters into human-readable bases and filters out any non
    ' RNA/DNA characters.
    '
    ' This is from the samtools documentation:
    '
    ' In the pileup format, each line represents a genomic position, consisting
    ' of chromosome name, 1-based coordinate, reference base, read bases, read
    ' qualities and alignment mapping qualities. Information on match,
    ' mismatch, indel, strand, mapping quality and start and end of a read are
    ' all encoded at the read base column. At this column, a dot stands for a
    ' match to the reference base on the forward strand, a comma for a match
    ' on the reverse strand, a ">" or "<" for a reference skip, "ACGTN" for a
    ' mismatch on the forward strand and "acgtn" for a mismatch on the reverse
    ' strand. A pattern "\+[0-9]+[ACGTNacgtn]+" indicates there is an insertion
    ' between this reference position and the next reference position. The
    ' length of the insertion is given by the integer in the pattern, followed
    ' by the inserted sequence. Similarly, a pattern "-[0-9]+[ACGTNacgtn]+"
    ' represents a deletion from the reference. The deleted bases will be
    ' presented as "*" in the following lines. Also at the read base column,
    ' a symbol "^" marks the start of a read. The ASCII of the character
    ' following "^" minus 33 gives the mapping quality. A symbol "$" marks the
    ' end of a read segment.
    '
    ' Note:  the samtools documentation is a bit out of date.  They now allow
    ' all of the IUPAC nucleotide codes for INDELS:
    ' [ACGTURYSWKMBDHVNacgturyswkmbdhvn].
    '
    ' We are converting all dots and commas to the upper case reference base.
    ' Even though the comma represents a match on the reverse strand, there is
    ' no need to take the complement of it, since samtools does that for us.
    ' We are converting all mismatches on the reverse strand to upper case as
    ' well, and again no complement is needed.
    '
    ' We are ignoring the following for now:
    ' 1) Reference skips (">" and "<")
    '
    ' aStringOfRawReads:
    '    A string representing the pile-up of read
    '    bases from a samtools mpileup command
    ' aStringOfRawBaseQuals:
    '    A string representing the raw base quality scores
    '    for the read bases from the mpileup command
    ' aStringOfRawMapQuals:
    '    A string representing the raw mapping quality
    '    scores for the reads from the mpileup command
    ' aReferenceBase:
    '    Used to convert "." and "," from the samtools mpileup command
    '''
    # Note:  Reverse strand mismatches have been
    #        reverse-complemented by samtools

    # initialize some counts
    starts = 0
    stops = 0
    insertions = 0
    deletions = 0
    finalBases = ""
    finalBaseQuals = ""
    finalMapQuals = ""
    currBaseIndex = 0
    currBaseQualIndex = 0
    currMapQualIndex = 0
    numBasesDict = collections.defaultdict(int)
    sumBaseQualsDict = collections.defaultdict(int)
    sumMapQualsDict = collections.defaultdict(int)
    numPlusStrandDict = collections.defaultdict(int)
    maxMapQualsDict = collections.defaultdict(int)
    sumMapQualZeroesDict = collections.defaultdict(int)

    # for testing:
    # aStringOfRawReads = 'T$TT+3AGG^".GT+2AG+2AG,-2AGGG..-1A<<>>'
    # aStringOfRawBaseQuals = "01234567890"
    # aStringOfRawMapQuals = "01234567890"
    if (anIsDebug):
        logging.debug("initBases=%s, initBaseQuals=%s, initMapQuals=%s",
                      aStringOfRawReads, aStringOfRawBaseQuals,
                      aStringOfRawMapQuals)

    # loop over each base in the pileups
    for index in xrange(0, len(aStringOfRawReads)):

        '''
        if (anIsDebug):
            logging.debug("index=%s, currBaseIndex=%s, " +
                          "currBaseQualIndex=%s, currMapQualIndex=%s",
                          str(index), str(currBaseIndex),
                          str(currBaseQualIndex), str(currMapQualIndex))
        '''

        # if we skipped ahead in the string due to an indel, then catch up here
        if index < currBaseIndex:
            continue

        # get the current base
        base = aStringOfRawReads[currBaseIndex]

        # if the currBaseQualIndex is >= the length of base qual
        # scores, then we've reached the end of base qual scores,
        # even though there could be more non-base characters in
        # aStringOfRawReads to process (e.g. <>$+2AG)
        if (currBaseQualIndex < len(aStringOfRawBaseQuals)):
            # the scores are in ascii, so convert them to integers
            rawBaseQual = aStringOfRawBaseQuals[currBaseQualIndex]
            convertedBaseQual = ord(rawBaseQual)-33

            # the scores are in ascii, so convert them to integers
            rawMapQual = aStringOfRawMapQuals[currMapQualIndex]
            convertedMapQual = ord(rawMapQual)-33

            '''
            if (anIsDebug):
                logging.debug("baseQual=%s, ord(baseQual)=%s, " +
                              "convertedBaseQual=%s, mapQual=%s, " +
                              "ord(mapQual)=%s, convertedMapQual=%s",
                              rawBaseQual, ord(rawBaseQual),
                              str(convertedBaseQual), rawMapQual,
                              ord(rawMapQual), str(convertedMapQual))
            '''
        else:
            convertedBaseQual = -1
            convertedMapQual = -1

        '''
        if (anIsDebug):
            logging.debug("index=%s, currBaseIndex=%s, currBase=%s, " +
                          "currBaseQual=%s, currMapQual=%s",
                          str(index), str(currBaseIndex), base,
                          str(convertedBaseQual), str(convertedMapQual))
        '''

        if base in "+-":
            # if we found an indel, count them and skip
            # ahead to the next base in the pileup
            # a pileup could look like:  "T$TT+3AGGGT+2AG+2AG.-2AGGG..-1A"
            # insertions start with a "+", deletions with a "-"
            # in theory, there could be multiple digits
            if base == "+":
                insertions += 1
            else:
                deletions += 1

            indelStart = currBaseIndex
            digitStart = currBaseIndex + 1
            digitEnd = currBaseIndex + 1
            # theoretically, there could be multiple digits
            # loop until we find the end of the digit
            for nextBase in aStringOfRawReads[digitStart:]:
                if nextBase.isdigit():
                    digitEnd += 1
                else:
                    break

            digit = aStringOfRawReads[digitStart:digitEnd]
            '''
            if (anIsDebug):
                logging.debug("digitStart=%s, digitEnd=%s, digit=%s",
                              str(digitStart), str(digitEnd), digit)
            '''
            # indelStart is where the + sign is
            # len(digit) since there could be multiple digits
            # int(digit) is the number of actual bases in the indel
            # +1 due to the exclusive end on lists
            indelEnd = indelStart + len(digit) + int(digit) + 1
            '''
            if (anIsDebug):
                logging.debug("indelStart=%s, indelEnd=%s, indel=%s",
                              indelStart, indelEnd,
                              aStringOfRawReads[indelStart:indelEnd])
            '''
            currBaseIndex = indelEnd
        elif base == "^":
            # skip over all start of read symbols "^"
            # (plus the following mapping quality score)
            # there are no base or mapping quality scores
            # for start symbols that need to be skipped
            currBaseIndex += 2
            starts += 1
        elif base == "$":
            # skip over all end of read symbols "$"
            # there are no base or mapping quality scores
            # for stop symbols that need to be skipped
            currBaseIndex += 1
            stops += 1
        elif base == ".":
            # a period represents the reference base on the plus strand
            base = aReferenceBase.upper()
            (finalBases,
             finalBaseQuals,
             finalMapQuals,
             numBasesDict,
             numPlusStrandDict,
             sumBaseQualsDict,
             sumMapQualsDict,
             sumMapQualZeroesDict,
             maxMapQualsDict) = convert_raw_base(base,
                                                 rawBaseQual,
                                                 rawMapQual,
                                                 convertedBaseQual,
                                                 convertedMapQual,
                                                 aMinBaseQual,
                                                 aMinMapQual,
                                                 finalBases,
                                                 finalBaseQuals,
                                                 finalMapQuals,
                                                 numBasesDict,
                                                 numPlusStrandDict,
                                                 sumBaseQualsDict,
                                                 sumMapQualsDict,
                                                 sumMapQualZeroesDict,
                                                 maxMapQualsDict,
                                                 True)

            currBaseIndex += 1
            currBaseQualIndex += 1
            currMapQualIndex += 1
        elif base == ",":
            # a comma represents the reference base on the negative strand
            base = aReferenceBase.upper()
            (finalBases,
             finalBaseQuals,
             finalMapQuals,
             numBasesDict,
             numPlusStrandDict,
             sumBaseQualsDict,
             sumMapQualsDict,
             sumMapQualZeroesDict,
             maxMapQualsDict) = convert_raw_base(base,
                                                 rawBaseQual,
                                                 rawMapQual,
                                                 convertedBaseQual,
                                                 convertedMapQual,
                                                 aMinBaseQual,
                                                 aMinMapQual,
                                                 finalBases,
                                                 finalBaseQuals,
                                                 finalMapQuals,
                                                 numBasesDict,
                                                 numPlusStrandDict,
                                                 sumBaseQualsDict,
                                                 sumMapQualsDict,
                                                 sumMapQualZeroesDict,
                                                 maxMapQualsDict,
                                                 False)

            currBaseIndex += 1
            currBaseQualIndex += 1
            currMapQualIndex += 1
        elif base in "AGCT":
            '''
            if (anIsDebug):
                logging.debug("base=%s, rawBaseQual=%s, " +
                              "ord(rawBaseQual)=%s, convertedBaseQual=%s, " +
                              "aMinBaseQual=%s", base, rawBaseQual,
                              ord(rawBaseQual), str(convertedBaseQual),
                              str(aMinBaseQual))
            '''

            # a non reference on the plus strand
            (finalBases,
             finalBaseQuals,
             finalMapQuals,
             numBasesDict,
             numPlusStrandDict,
             sumBaseQualsDict,
             sumMapQualsDict,
             sumMapQualZeroesDict,
             maxMapQualsDict) = convert_raw_base(base,
                                                 rawBaseQual,
                                                 rawMapQual,
                                                 convertedBaseQual,
                                                 convertedMapQual,
                                                 aMinBaseQual,
                                                 aMinMapQual,
                                                 finalBases,
                                                 finalBaseQuals,
                                                 finalMapQuals,
                                                 numBasesDict,
                                                 numPlusStrandDict,
                                                 sumBaseQualsDict,
                                                 sumMapQualsDict,
                                                 sumMapQualZeroesDict,
                                                 maxMapQualsDict,
                                                 True)

            currBaseIndex += 1
            currBaseQualIndex += 1
            currMapQualIndex += 1
        elif base in "agct":
            '''
            if (anIsDebug):
                logging.debug("base=%s, rawBaseQual=%s, " +
                              "ord(rawBaseQual)=%s, convertedBaseQual=%s, " +
                              "aMinBaseQual=%s", base, rawBaseQual,
                              ord(rawBaseQual), str(convertedBaseQual),
                              str(aMinBaseQual))
            '''
            # a non reference on the negative strand
            base = base.upper()
            (finalBases,
             finalBaseQuals,
             finalMapQuals,
             numBasesDict,
             numPlusStrandDict,
             sumBaseQualsDict,
             sumMapQualsDict,
             sumMapQualZeroesDict,
             maxMapQualsDict) = convert_raw_base(base,
                                                 rawBaseQual,
                                                 rawMapQual,
                                                 convertedBaseQual,
                                                 convertedMapQual,
                                                 aMinBaseQual,
                                                 aMinMapQual,
                                                 finalBases,
                                                 finalBaseQuals,
                                                 finalMapQuals,
                                                 numBasesDict,
                                                 numPlusStrandDict,
                                                 sumBaseQualsDict,
                                                 sumMapQualsDict,
                                                 sumMapQualZeroesDict,
                                                 maxMapQualsDict,
                                                 False)

            currBaseIndex += 1
            currBaseQualIndex += 1
            currMapQualIndex += 1
        else:
            # we are ignoring the base 'N' or 'n'
            currBaseIndex += 1
            currBaseQualIndex += 1
            currMapQualIndex += 1

    if (anIsDebug):
        logging.debug("finalBases=%s, finalBaseQuals=%s, finalMapQuals=%s",
                      finalBases, finalBaseQuals, finalMapQuals)

    # get the lengths
    lenFinalBases = len(finalBases)
    lenFinalBaseQuals = len(finalBaseQuals)
    lenFinalMapQuals = len(finalMapQuals)

    # at this point, the length of the pileups string should
    # be equal to the length of the quality scores
    if (lenFinalBases != lenFinalBaseQuals):
        logging.error("Traceback: convert_and_filter_raw_reads() Error at " +
                      "coordinate %s:%s.  The length %s of the final pileup " +
                      "of reads is != the length %s of the final base " +
                      "quality scores. Original Pileup=%s, Final Pileup=%s, " +
                      "Original BaseQualScores=%s, Final BaseQualScores=%s",
                      aChr, str(aCoordinate), lenFinalBases,
                      lenFinalBaseQuals, aStringOfRawReads, finalBases,
                      aStringOfRawBaseQuals, finalBaseQuals)
    if (lenFinalBases != lenFinalMapQuals):
        logging.error("Traceback: convert_and_filter_raw_reads() Error at " +
                      "coordinate %s:%s.  The length %s of the final pileup " +
                      "of reads is != the length %s of the final mapping " +
                      "quality scores. Original Pileup=%s, Final Pileup=%s, " +
                      "Original MapQualScores=%s, Final MapQualScores=%s",
                      aChr, str(aCoordinate), lenFinalBases,
                      lenFinalBaseQuals, aStringOfRawReads, finalBases,
                      aStringOfRawMapQuals, finalMapQuals)

    return (finalBases, finalBaseQuals, finalMapQuals, lenFinalBases,
            starts, stops, insertions, deletions, numBasesDict,
            sumBaseQualsDict, sumMapQualsDict, sumMapQualZeroesDict,
            maxMapQualsDict, numPlusStrandDict)


def format_bam_output(aChrom, aRefList, anAltList, anAltCountsDict,
                      anAltPerDict, aNumBases, aStartsCount, aStopsCount,
                      anInsCount, aDelCount, aBaseCountsDict,
                      aBaseQualSumsDict, aMapQualSumsDict, aMapQualZeroesDict,
                      aMapQualMaxesDict, aPlusStrandCountsDict, aGTMinDepth,
                      aGTMinPct, aBamOutputString, anIsDebug):
    '''
    ' This function converts information from a .bam mpileup coordinate into a
    ' format that can be output to a VCF formatted file. This function
    ' calculates the average overall base quality score, strand bias, and
    ' fraction of reads supporting the alternative. It also calculates the
    ' allele specific depth, average base quality score, strand bias, and
    ' fraction of reads supporting the alternative. The format for the output
    ' in VCF is:  GT:DP:AD:AF:INS:DEL:DP4:START:STOP:MQ0:MMQ:MQ:BQ:SB:MMP.
    '
    ' aDnaSet:
    '    A set of dna found at this position
    ' anAltList:
    '    A list of alternative alleles found thus far
    ' aStringReads:
    '    A string of reads that have been converted from
    '    raw format and filtered
    ' aStringQualScores:
    '    A string of quality scores for the reads
    ' aStartsCount:
    '    The number of bases that were at the start of the read
    ' aStopsCount:
    '    The number of bases that were at the stop of the read
    ' anInsCount:
    '    The number of insertions at this position
    ' aDelCount:
    '    The number of deletions at this position
    ' aBaseCountsDict:
    '    A dictionary with the number of bases of each type
    ' aBaseQualSumsDict:
    '    A dictionary with the sum of all base quality scores for each allele
    ' aMapQualSumsDict:
    '    A dictionary with the sum of all map quality scores for each allele
    ' aMapQualMaxesDict:
    '    A dictionary with the maximum mapping quality for each allele
    ' aMapQualZeroesDict:
    '    A dictionary with the number of reads with a mapping score of zero
    '    for each allele
    ' aPlusStrandCountsDict:
    '    The number of bases that occurred on the plus strand
    '''

    # initialize the return variables
    sumAltReadSupport = 0

    # if we have reads at this position
    if (aNumBases > 0):

        # format = "GT:DP:AD:AF:INS:DEL:DP4:START:STOP:MQ0:MMQ:MQA:BQ:SB:MMP"

        # ##FORMAT=<ID=GT,Number=1,Type=String,
        #    Description="Genotype">
        # ##FORMAT=<ID=DP,Number=1,Type=Integer,
        #    Description="Read depth at this location in the sample">
        # ##FORMAT=<ID=AD,Number=R,Type=Integer,
        #    Description="Depth of reads supporting allele">
        # ##FORMAT=<ID=AF,Number=R,Type=Float,
        #    Description="Fraction of reads supporting allele">
        # ##FORMAT=<ID=INS,Number=1,Type=Integer,
        #    Description="Number of small insertions at this location">
        # ##FORMAT=<ID=DEL,Number=1,Type=Integer,
        #    Description="Number of small deletions at this location">
        # ##FORMAT=<ID=START,Number=1,Type=Integer,
        #    Description="Number of reads starting at this location">
        # ##FORMAT=<ID=STOP,Number=1,Type=Integer,
        #    Description="Number of reads stopping at this location">
        # ##FORMAT=<ID=MQ0,Number=R,Type=Integer,
        #    Description="Number of mapping quality zero
        #                 reads harboring allele">
        # ##FORMAT=<ID=MMQ,Number=R,Type=Integer,
        #    Description="Maximum mapping quality of read harboring allele">
        # ##FORMAT=<ID=MQA,Number=R,Type=Integer,
        #    Description="Avg mapping quality for reads supporting allele">
        # ##FORMAT=<ID=BQ,Number=R,Type=Integer,
        #    Description="Avg base quality for reads supporting allele">
        # ##FORMAT=<ID=SB,Number=R,Type=Float,
        #    Description="Strand Bias for reads supporting allele">
        # ##FORMAT=<ID=MMP,Number=R,Type=Float,
        #    Description="Multi-mapping percent for reads supporting allele">
        # ##FORMAT=<ID=DP4,Number=.,Type=Integer,
        #    Description="Number of high-quality ref-forward, ref-reverse,
        #                 alt-forward and alt-reverse bases">

        # initialize some lists
        depths = list()
        depths4 = list()
        freqs = list()
        baseQuals = list()
        mapQuals = list()
        mapQualZeroes = list()
        mapQualMaxes = list()
        strandBias = list()
        mmps = list()
        altCountsDict = {}

        # for each base in the ref list and alt list
        # the order matters for the output
        for base in (aRefList + anAltList):

            # get the number of times the base occurs
            count = aBaseCountsDict[base]
            depths.append(count)

            # get the DP4 depths
            depths4.append(aPlusStrandCountsDict[base])
            depths4.append(count-aPlusStrandCountsDict[base])

            # calculate the allele specific fraction of read support
            alleleFreq = round(count/float(aNumBases), 2)
            freqs.append(alleleFreq)

            # if the base is an alt, then count it for the overall read support
            if (base in anAltList):
                sumAltReadSupport += count
                anAltCountsDict[base] += count
                # we need just the alt counts for the genotypes code below
                altCountsDict[base] = count

            # calculate the allele specific avg base quality,
            # map quality and strand bias scores
            if (count > 0):
                avgBaseQual = aBaseQualSumsDict[base]/float(count)
                avgBaseQuality = int(round(avgBaseQual, 0))
                avgMapQual = aMapQualSumsDict[base]/float(count)
                avgMapQuality = int(round(avgMapQual, 0))
                avgSbias = aPlusStrandCountsDict[base]/float(count)
                avgPlusStrandBias = round(avgSbias, 2)
            else:
                avgBaseQuality = 0
                avgMapQuality = 0
                avgPlusStrandBias = 0.0

            baseQuals.append(avgBaseQuality)
            mapQuals.append(avgMapQuality)
            mapQualMaxes.append(aMapQualMaxesDict[base])
            mapQualZeroes.append(aMapQualZeroesDict[base])
            strandBias.append(avgPlusStrandBias)
            mmps.append(".")

        # get the genotype:
        #    if chrom Y
        #        then genotype = the ref or alt with the max read depth
        #    if there are only reads for ref
        #        then genotype = 0/0
        #    if there are only reads for alt
        #        then genotype = 1/1
        #    if there are reads for both ref and alt above the min depth
        #    and percent, then pick the ones with max counts
        #        then genotype = 0/1
        #    if chrom M or MT
        #        then any allele above the min depth and percent can be listed
        genotypes = None
        refAltList = aRefList + anAltList
        singleGenotypeChroms = ["chrY", "Y"]
        mChroms = ["chrM", "chrMT", "M", "MT"]

        # if it is a single chrom, then we can only assign one allele for the
        # genotype. if one of the alts has a depth and percent above the mins,
        # then use it, otherwise use the ref
        if (aChrom in singleGenotypeChroms):
            if aBaseCountsDict:

                # get the total depth
                totalDepth = sum(aBaseCountsDict.itervalues())

                # if we have some alts
                if altCountsDict:
                    # find the max alt allele
                    (maxAltBase, maxAltDepth) = max(altCountsDict.iteritems(),
                                                    key=lambda x:x[1])
                    maxAltPct = round(maxAltDepth/float(totalDepth), 2)

                    # if the max alt depth is large enough
                    if (maxAltDepth >= aGTMinDepth and maxAltPct >= aGTMinPct):
                        # find the index for the max depth on the original list
                        maxAltIndex = refAltList.index(maxAltBase)
                    else:
                        # it wasn't large enough, so just use the ref
                        maxAltIndex = 0
                else:
                    # no alts, so just use the ref
                    maxAltIndex = 0

                # set the single genotype
                genotypes = [maxAltIndex]

            else:
                # we don't have any bases, so just set it to the ref
                genotypes = [0]

        # if it is an M chrom, then we can assign as many alleles as we want
        # for the genotype. for all bases with a depth and percent above the
        # mins, set the genotype
        elif (aChrom in mChroms):
            if aBaseCountsDict:

                # get the total depth
                totalDepth = sum(aBaseCountsDict.itervalues())

                tmpGenotypes = []
                # for each base in the ref and alt
                for (base, depth) in aBaseCountsDict.iteritems():
                    # calculate the percent
                    percent = round(depth/float(totalDepth), 2)
                    # if the max alt depth and percent are large enough
                    if (depth >= aGTMinDepth and percent >= aGTMinPct):
                        # add the index to the list
                        index = refAltList.index(base)
                        tmpGenotypes.append(index)

                # if nothing passed the mins, then just take the ref
                if (len(tmpGenotypes) == 0):
                    tmpGenotypes = [0]

                genotypes = sorted(tmpGenotypes)
            else:
                # we don't have any bases, so just set it to the ref
                genotypes = [0]

        # if it is a diploid chrom, then assign the
        # 2 max counts above the min cutoffs
        else:
            # get the total depth
            totalDepth = sum(aBaseCountsDict.itervalues())

            # make a copy of the dict to manipulate
            baseCountsTmpDict = dict(aBaseCountsDict)

            # get the max depth
            (max1Base, max1Depth) = max(baseCountsTmpDict.iteritems(),
                                        key=lambda x:x[1])

            # find the index for the max depth on the original list
            max1DepthIndex = refAltList.index(max1Base)

            # remove the max from the tmp list
            del baseCountsTmpDict[max1Base]

            # if we still have some depths, find the 2nd max
            if baseCountsTmpDict:

                # get the max depth
                (max2Base, max2Depth) = max(baseCountsTmpDict.iteritems(),
                                            key=lambda x:x[1])
                max2Pct = round(max2Depth/float(totalDepth), 2)

                # if the max depth is large enough
                if (max2Depth >= aGTMinDepth and max2Pct >= aGTMinPct):
                    # find the index for the max depth base on the original
                    # list. note: here we are using the dictionary of
                    # base=count, so we can specifically ask for the base.
                    # In subsequence genotypes() methods, we only have the
                    # depths without the corresponding base so we have to
                    # pay extra attention when the depths are equal
                    max2DepthIndex = refAltList.index(max2Base)
                else:
                    # it wasn't large enough, so just use previous max
                    max2DepthIndex = max1DepthIndex

            else:
                # otherwise it's the same as the first
                max2DepthIndex = max1DepthIndex

            genotypes = sorted([max1DepthIndex, max2DepthIndex])

        # create a list of each of the elements, then join them by colon
        outputList = ("/".join(map(str, genotypes)),
                      str(aNumBases),
                      ",".join(map(str, depths)),
                      ",".join(map(str, freqs)),
                      str(anInsCount),
                      str(aDelCount),
                      ",".join(map(str, depths4)),
                      str(aStartsCount),
                      str(aStopsCount),
                      ",".join(map(str, mapQualZeroes)),
                      ",".join(map(str, mapQualMaxes)),
                      ",".join(map(str, mapQuals)),
                      ",".join(map(str, baseQuals)),
                      ",".join(map(str, strandBias)),
                      ",".join(mmps))
        aBamOutputString = ":".join(outputList)

    # return the string representation and overall calculations
    return (aBamOutputString, anAltCountsDict, anAltPerDict, sumAltReadSupport)


def get_next_pileup(aGenerator):
    '''
    ' This function returns the next pileup from a generator that yields
    ' pileups.  If the user doesn't specify all four BAM files, then the
    ' generator will be "None", so just return some default values. If we
    ' reach the end of a file, the generator will throw a StopIteration,
    ' so just catch it and return some default values.  Otherwise, return
    ' the appropriate pileup information.
    '
    ' aGenerator:  A .bam mpileup generator that yields the next pileup
    '''

    if (aGenerator is None):
        return False, "", -1, "", 0, "", "", ""
    else:
        try:
            # get the next line
            (chrom, coordinate, refBase, numReads,
             reads, baseQuals, mapQuals) = aGenerator.next()
            return (True, chrom, int(coordinate), refBase,
                    int(numReads), reads, baseQuals, mapQuals)
        except StopIteration:
            return False, "", -1, "", 0, "", "", ""


def find_variants(aChr, aCoordinate, aRefBase, aNumBases, aReads, aBaseQuals,
                  aMapQuals, aPreviousUniqueBases, aPreviousBaseCounts,
                  aReadDepthDict, anAltPerDict, aCoordinateWithData, aDnaSet,
                  aRefList, anAltList, anAltCountsDict, aHasValidData,
                  aShouldOutput, aGainModCount, aLossModCount, aGainModType,
                  aLossModType, anInfoDict, aMinTotalNumBases, aMinAltNumBases,
                  aPreviousMinAltNumBases, aMinBaseQual, aMinMapQual,
                  aSourcePrefix, aGTMinDepth, aGTMinPct, aBamOutputString,
                  anIsDebug):
    '''
    ' This function finds variants in BAM pileups.  This function first
    ' converts the samtools pileup of reads into human-readable reads and then
    ' records some characteristics of the pileups.  It counts the number of
    ' bases on the plus and minus strands, the number of bases at the start
    ' and end of reads, and the number of indels.  This function then ensures
    ' that the bases in the reads pass the minimum base quality score. If the
    ' number of remaining bases is greater than or equal to the minimum total
    ' of bases specified by 'aMinTotalNumBases', then this function looks to
    ' see if there are any variants in the data.
    '
    ' The 'aDnaSet' object is empty when processing normal DNA.  This function
    ' automatically adds the reference base, so no pre-processing of aDnaSet
    ' is needed.  After the reference has been added, the function looks for
    ' variants in the reads.  If a base is not in aDnaSet and there are at
    ' least 'aMinAltNumBases' of them, then this function adds the variant to
    ' 'aModTypesSet'.  After all the variants have been processed, the unique
    ' reads at this position are added to 'aDnaSet' which is used in the next
    ' steps: looking for somatic variations and rna-editing events.
    '
    ' This function returns:
    ' bamOutputString - The FORMAT field for the pileup data at this position
    ' aDnaSet - A set of 'parent' DNA:
    '    - Ref for germline variants,
         - Ref + normal for somatic mutations,
         - Ref + normal + tumor for rna-editing
    ' aHasValidData - If there was valid data at this position
    ' aShouldOutput - If there were any variants found to be output
    ' aModCount - The number of variants found
    ' aModTypesSet - The set of variants found
    '''

    # default outputs
    sumOfBaseQuals = 0
    sumOfMapQuals = 0
    sumOfMapQualZeroes = 0
    sumOfStrandBiases = 0
    sumOfAltReads = 0
    oneAboveMinAltBasesFlag = False
    setBelowMinAltBasesFlag = False
    uniqueBases = ""
    baseCountsDict = collections.defaultdict(int)
    insCount = 0
    delCount = 0
    starts = 0
    stops = 0

    if (anIsDebug):
        logging.debug("aRefBase=%s, aDnaSet=%s, aPreviousMinAltNumBases=%s, " +
                      "aMinAltNumBases=%s, aPreviousBaseCounts=%s",
                      aRefBase, aDnaSet, aPreviousMinAltNumBases,
                      aMinAltNumBases, aPreviousBaseCounts)

    # if we still have some bases
    if (aNumBases > 0):

        # convert and filter out the bases that
        # don't meet the minimum requirements
        (convertedReads,
         convertedBaseQuals,
         convertedMapQuals,
         aNumBases,
         starts,
         stops,
         insCount,
         delCount,
         baseCountsDict,
         sumBaseQualsDict,
         sumMapQualsDict,
         sumMapQualZeroesDict,
         maxMapQualsDict,
         plusStrandCountsDict) = convert_and_filter_raw_reads(aChr,
                                                              aCoordinate,
                                                              aReads,
                                                              aBaseQuals,
                                                              aMapQuals,
                                                              aRefBase,
                                                              aMinBaseQual,
                                                              aMinMapQual,
                                                              anIsDebug)

        if (anIsDebug):
            logging.debug("After convert_and_filter_raw_reads() on %s: %s " +
                          "%s %s %s %s %s %s %s %s %s %s %s %s %s %s %s %s",
                          aSourcePrefix, aChr, aCoordinate, aRefBase,
                          aNumBases, convertedReads, convertedBaseQuals,
                          convertedMapQuals, starts, stops, insCount, delCount,
                          baseCountsDict, sumBaseQualsDict, sumMapQualsDict,
                          sumMapQualZeroesDict, maxMapQualsDict,
                          plusStrandCountsDict)

        # if we still have some bases
        if (aNumBases > 0):
            aCoordinateWithData += 1

            # if the dna set is empty, then none of the previous samples had
            # data add the reference below, and set all bases to equal
            # aMinAltNumBases because we assume that the reference or "neutral"
            # sample had enough previous bases for all bases
            if (len(aDnaSet) == 0):
                for base in ('ACTG'):
                    # we can do this, b/c baseCountsDict gets returned as the
                    # previousCounts for this sample.  we can't do this if
                    # aPreviousBaseCounts should hold the counts for all
                    # previous samples.
                    # aPreviousBaseCounts[base] = aMinAltNumBases
                    aPreviousBaseCounts[base] = aPreviousMinAltNumBases

            # add the reference base
            aDnaSet.add(aRefBase)
            # we always have enough of the ref base
            # aPreviousBaseCounts[aRefBase] = aMinAltNumBases
            aPreviousBaseCounts[aRefBase] = aPreviousMinAltNumBases

            # for each unique base
            for base in set(convertedReads):

                # keep track of every ALT base in the order that it's found
                if (base not in aDnaSet and base not in anAltList):
                    anAltList.append(base)

                # if we have enough total bases
                if (aNumBases >= aMinTotalNumBases):

                    aHasValidData = True

                    # keep track of every unique base
                    uniqueBases += base

                    # if there is a base that wasn't in the previous sample,
                    # or the base was in the previous sample, but there weren't
                    # enough to make a call
                    # if ((base not in aDnaSet) or
                    #    (aPreviousBaseCounts[base] < aMinAltNumBases)):
                    if ((base not in aDnaSet) or
                        (aPreviousBaseCounts[base] < aPreviousMinAltNumBases)):

                        # if we have enough ALT bases
                        if (baseCountsDict[base] >= aMinAltNumBases):
                            oneAboveMinAltBasesFlag = True
                            aShouldOutput = True
                            # aGainModCount += 1

                            if (anIsDebug):
                                logging.debug("Modification found!  Base " +
                                              "'%s' does not exist in the " +
                                              "parent DNA %s or the parent " +
                                              "count %s was not above the " +
                                              "min %s", base, aDnaSet,
                                              str(aPreviousBaseCounts[base]),
                                              str(aPreviousMinAltNumBases))

                            # if this is the first modification found,
                            # then record it in the "SS" field
                            if (len(anInfoDict["SS"]) == 0):
                                # germline
                                if (aGainModType == "GERM"):
                                    anInfoDict["SS"].append("1")
                                # somatic
                                elif (aGainModType == "SOM"):
                                    anInfoDict["SS"].append("2")
                                    anInfoDict["SOMATIC"].append("True")
                                # rna-editing
                                elif (aGainModType.find("EDIT") != -1):
                                    anInfoDict["SS"].append("4")
                                # unknown
                                else:
                                    anInfoDict["SS"].append("5")

                            # it didn't match anything so far
                            for dna in aDnaSet:
                                # check to see which parent bases
                                # were above the minimum
                                prevCount = aPreviousBaseCounts[dna]
                                # if (prevCount >= aMinAltNumBases):
                                if (prevCount >= aPreviousMinAltNumBases):
                                    anInfoDict["MT"].append(aGainModType)
                                    anInfoDict["MC"].append(dna + ">" + base)
                                    aGainModCount += 1
                        else:
                            setBelowMinAltBasesFlag = True

            # add the unique reads for the next step
            aDnaSet = aDnaSet.union(set(convertedReads))

            # get the summary output for the pileups at this position
            (aBamOutputString,
             anAltCountsDict,
             anAltPerDict,
             sumOfAltReads) = format_bam_output(aChr,
                                                aRefList,
                                                anAltList,
                                                anAltCountsDict,
                                                anAltPerDict,
                                                aNumBases,
                                                starts,
                                                stops,
                                                insCount,
                                                delCount,
                                                baseCountsDict,
                                                sumBaseQualsDict,
                                                sumMapQualsDict,
                                                sumMapQualZeroesDict,
                                                maxMapQualsDict,
                                                plusStrandCountsDict,
                                                aGTMinDepth,
                                                aGTMinPct,
                                                aBamOutputString,
                                                anIsDebug)

            sumOfBaseQuals = sum(sumBaseQualsDict.itervalues())
            sumOfMapQuals = sum(sumMapQualsDict.itervalues())
            sumOfMapQualZeroes = sum(sumMapQualZeroesDict.itervalues())
            sumOfStrandBiases = sum(plusStrandCountsDict.itervalues())

    return (aBamOutputString, uniqueBases, baseCountsDict, aReadDepthDict,
            anAltPerDict, aCoordinateWithData, aDnaSet, anAltList,
            anAltCountsDict, aHasValidData, aShouldOutput,
            (aNumBases < aMinTotalNumBases),
            (not oneAboveMinAltBasesFlag and setBelowMinAltBasesFlag),
            aGainModCount, aLossModCount, anInfoDict, aNumBases, insCount,
            delCount, starts, stops, sumOfBaseQuals, sumOfMapQuals,
            sumOfMapQualZeroes, sumOfStrandBiases, sumOfAltReads)


def output_vcf_header(anOutputFileHandler, aVCFFormat, aRefId, aRefURL,
                      aRefFilename, aFastaFilename, aRadiaVersion, aPatientId,
                      aParamDict, aFilenameList, aLabelList, aDescList,
                      aPlatformList, aSourceList, anAnalytesList, aDisease):
    '''
    ' This function creates a VCF header that is used for the output.
    '
    ' anOutputFileHandler - Where the header should be output
    ' aVCFFormat - The current file format version
    ' aRefId - The short reference id such hg18, hg19, GRCh37, hg38, GRCh38
    ' aRefURL - The URL for the reference file provided
    ' aRefFilename - The filename of the reference
    ' aRadiaVersion - The version of RADIA
    ' aPatientId - The unique patient Id to be used in the SAMPLE tag
    ' aParamDict - Used to record the parameters that were used to run RADIA
    ' aFilenameList - Used in the SAMPLE tag
    ' aLabelList - Used in the SAMPLE tag
    ' aDescList - Used in the SAMPLE tag
    ' aPlatformList - Used in the SAMPLE tag
    ' aSourceList - Used in the SAMPLE tag
    '''

    # initialize the column headers
    columnHeaders = ["CHROM", "POS", "ID", "REF", "ALT",
                     "QUAL", "FILTER", "INFO", "FORMAT"]

    # output the initial fields
    anOutputFileHandler.write("##fileformat=" + aVCFFormat + "\n")
    anOutputFileHandler.write("##tcgaversion=1.0\n")
    if (aDisease is not None):
        anOutputFileHandler.write("##disease=" + aDisease + "\n")
    anOutputFileHandler.write(
        "##fileDate=" + datetime.date.today().strftime("%Y%m%d") + "\n")
    anOutputFileHandler.write(
        "##source=\"RADIA pipeline " + aRadiaVersion + "\"\n")

    # output the reference information
    if (aRefId is not None):
        if (aRefFilename is not None):
            anOutputFileHandler.write(
                "##reference=<ID=" + aRefId +
                ",Source=file:" + aRefFilename + ">\n")
        elif (aFastaFilename is not None):
            anOutputFileHandler.write(
                "##reference=<ID=" + aRefId +
                ",Source=file:" + aFastaFilename + ">\n")
    elif (aRefFilename is not None):
        anOutputFileHandler.write("##reference=file:" + aRefFilename + "\n")
    else:
        anOutputFileHandler.write("##reference=file:" + aFastaFilename + "\n")

    # output the URL or the fasta to the assembly tag
    if (aRefURL is not None):
        anOutputFileHandler.write("##assembly=" + aRefURL + "\n")
    else:
        anOutputFileHandler.write("##assembly=file:" + aFastaFilename + "\n")

    anOutputFileHandler.write("##phasing=none\n")

    # add RADIA param info
    aParamDict["algorithm"] = "RADIA"
    aParamDict["version"] = "1.1.4"

    # output the vcf generator tag
    generator = "##vcfGenerator=<"
    for (paramName) in sorted(aParamDict.iterkeys()):
        paramValue = aParamDict[paramName]
        if (paramValue is not None):
            # don't output the defaults for files that aren't specified
            if (paramName.startswith("dnaNormal") and
                "DNA_NORMAL" not in aLabelList):
                continue
            elif (paramName.startswith("rnaNormal") and
                  "RNA_NORMAL" not in aLabelList):
                continue
            elif (paramName.startswith("dnaTumor") and
                  "DNA_TUMOR" not in aLabelList):
                continue
            elif (paramName.startswith("rnaTumor") and
                  "RNA_TUMOR" not in aLabelList):
                continue
            else:
                if (type(paramValue) is str and " " in paramValue):
                    generator += paramName + "=<\"" + str(paramValue) + "\">,"
                else:
                    generator += paramName + "=<" + str(paramValue) + ">,"

    generator = generator.rstrip(",")
    generator += ">\n"
    anOutputFileHandler.write(generator)

    anOutputFileHandler.write("##INDIVIDUAL=" + aPatientId + "\n")

    # get the sample fields
    samples = ""
    for (filename, label,
         description, platform,
         source, analyte) in izip(aFilenameList, aLabelList, aDescList,
                                  aPlatformList, aSourceList, anAnalytesList):

        # try to get the TCGA barcode from the filename for the header
        # tcgaRegEx = re.compile("TCGA-(\\w){2}-(\\w){4}-(\\w){3}-(\\w){3}")
        matchObj = i_tcgaNameRegEx.search(filename)
        if (matchObj is not None):
            samples += ("##SAMPLE=<ID=" + label +
                        ",SampleName=" + matchObj.group() +
                        ",Individual=" + aPatientId +
                        ",Description=\"" + description +
                        "\",File=\"" + filename +
                        "\",Analyte=\"" + analyte + "\",")
        else:
            samples += ("##SAMPLE=<ID=" + label +
                        ",SampleName=" + aPatientId +
                        ",Individual=" + aPatientId +
                        ",Description=\"" + description +
                        "\",File=\"" + filename +
                        "\",Analyte=\"" + analyte + "\",")

        if (platform is not None):
            samples += "Platform=\"" + platform + "\","
        if (source is not None):
            samples += "Source=\"" + source + "\","

        samples = samples.rstrip(",")
        samples += ">\n"
        columnHeaders.append(label)

    anOutputFileHandler.write(samples)

    # add pedigree tags for latest snpEff version
    for label in aLabelList:
        if (label == "DNA_TUMOR"):
            anOutputFileHandler.write("##PEDIGREE=<Derived=DNA_TUMOR," +
                                      "Original=DNA_NORMAL>\n")
        elif (label == "RNA_TUMOR"):
            anOutputFileHandler.write("##PEDIGREE=<Derived=RNA_TUMOR," +
                                      "Original=DNA_NORMAL>\n")
        elif (label == "RNA_NORMAL"):
            anOutputFileHandler.write("##PEDIGREE=<Derived=RNA_NORMAL," +
                                      "Original=DNA_NORMAL>\n")

    # get the info fields
    anOutputFileHandler.write(
        "##INFO=<ID=NS,Number=1,Type=Integer,Description=\"Number of " +
        "samples with data\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=AN,Number=1,Type=Integer,Description=\"Number of " +
        "unique alleles across all samples\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=AC,Number=A,Type=Integer,Description=\"Allele count " +
        "in genotypes, for each ALT allele, in the same order as listed\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=AF,Number=A,Type=Float,Description=\"Allele frequency, " +
        "for each ALT allele, in the same order as listed\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Total read " +
        "depth across all samples\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=INS,Number=1,Type=Integer,Description=\"Number of small " +
        "insertions at this location across all samples\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=DEL,Number=1,Type=Integer,Description=\"Number of small " +
        "deletions at this location across all samples\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=START,Number=1,Type=Integer,Description=\"Number of " +
        "reads starting at this location across all samples\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=STOP,Number=1,Type=Integer,Description=\"Number of " +
        "reads stopping at this location across all samples\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=BQ,Number=1,Type=Integer,Description=\"Overall " +
        "average base quality\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=MQ,Number=1,Type=Integer,Description=\"Overall " +
        "average mapping quality\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=MQ0,Number=1,Type=Integer,Description=\"Total " +
        "Mapping Quality Zero Reads\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=SB,Number=1,Type=Float,Description=\"Overall " +
        "strand bias\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=FA,Number=1,Type=Float,Description=\"Overall " +
        "fraction of reads supporting ALT\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=MT,Number=.,Type=String,Description=\"Modification " +
        "types at this location\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=MC,Number=.,Type=String,Description=\"Modification " +
        "base changes at this location\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=MF,Number=.,Type=String,Description=\"Modification " +
        "filters applied to the filter types listed in MFT\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=MFT,Number=.,Type=String,Description=\"Modification " +
        "filter types at this location with format " +
        "origin_modType_modChange\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=SOMATIC,Number=0,Type=Flag,Description=\"Indicates " +
        "if record is a somatic mutation\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=SS,Number=1,Type=Integer,Description=\"Variant " +
        "status relative to non-adjacent Normal,0=wildtype,1=germline," +
        "2=somatic,3=LOH,4=post-transcriptional modification,5=unknown\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=SSC,Number=1,Type=Integer,Description=\"Somatic score " +
        "in Phred scale (0-255) derived from p-value\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=PVAL,Number=1,Type=Float,Description=\"Fisher's " +
        "Exact Test P-value\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=SST,Number=1,Type=String,Description=\"Somatic " +
        "status of variant\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=VT,Number=1,Type=String,Description=\"Variant type, " +
        "can be SNP, INS or DEL\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=PN,Number=1,Type=String,Description=\"Previous " +
        "nucleotide in reference sequence\">\n")
    anOutputFileHandler.write(
        "##INFO=<ID=NN,Number=1,Type=String,Description=\"Next " +
        "nucleotide in reference sequence\">\n")

    # get the filter fields
    anOutputFileHandler.write(
        "##FILTER=<ID=noref,Description=\"Position skipped, reference=N\">\n")
    anOutputFileHandler.write(
        "##FILTER=<ID=diffref,Description=\"Position skipped, " +
        "different references in files\">\n")
    anOutputFileHandler.write(
        "##FILTER=<ID=mbt,Description=\"Total bases is " +
        "less than the minimum\">\n")
    anOutputFileHandler.write(
        "##FILTER=<ID=mba,Description=\"ALT bases is " +
        "less than the minimum\">\n")

    # get the format fields
    # these fields are sample specific
    anOutputFileHandler.write(
        "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=DP,Number=1,Type=Integer,Description=\"Read depth " +
        "at this location in the sample\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=AD,Number=R,Type=Integer,Description=\"Depth of " +
        "reads supporting allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=AF,Number=R,Type=Float,Description=\"Fraction of " +
        "reads supporting allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=INS,Number=1,Type=Integer,Description=\"Number of " +
        "small insertions at this location\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=DEL,Number=1,Type=Integer,Description=\"Number of " +
        "small deletions at this location\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=START,Number=1,Type=Integer,Description=\"Number of " +
        "reads starting at this location\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=STOP,Number=1,Type=Integer,Description=\"Number of " +
        "reads stopping at this location\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=MQ0,Number=R,Type=Integer,Description=\"Number of " +
        "mapping quality zero reads harboring allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=MMQ,Number=R,Type=Integer,Description=\"Maximum " +
        "mapping quality of read harboring allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=MQA,Number=R,Type=Integer,Description=\"Avg mapping " +
        "quality for reads supporting allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=BQ,Number=R,Type=Integer,Description=\"Avg base " +
        "quality for reads supporting allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=SB,Number=R,Type=Float,Description=\"Strand Bias " +
        "for reads supporting allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=MMP,Number=R,Type=Float,Description=\"Multi-mapping " +
        "percent for reads supporting allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=DP4,Number=.,Type=Integer,Description=\"Number of " +
        "high-quality ref-forward, ref-reverse, alt-forward and " +
        "alt-reverse bases\">\n")

    '''
    anOutputFileHandler.write(
        "##FORMAT=<ID=SS,Number=1,Type=Integer,Description=\"Variant " +
        "status relative to non-adjacent Normal, 0=wildtype,1=germline," +
        "2=somatic,3=LOH,4=post-transcriptional modification,5=unknown\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=SSC,Number=1,Type=Integer,Description=\"Somatic " +
        "score between 0 and 255\">\n")
    # across the whole read, what is the avg base quality of all mismatches
    anOutputFileHandler.write(
        "##FORMAT=<ID=MMQS,Number=R,Type=Float,Description=\"Average " +
        "mismatch quality sum of reads harboring allele\">\n")
    anOutputFileHandler.write(
        "##FORMAT=<ID=MQ,Number=1,Type=Integer,Description=\"Phred style " +
        "probability score that the variant is novel with respect " +
        "to the genome's ancestor\">\n")
    '''

    anOutputFileHandler.write("#" + "\t".join(columnHeaders) + "\n")

    return


def pad(aList, aPadder, aLength):
    '''
    ' This function pads a list with the value specified in the aPadder
    ' variable to the length specified in the aLength variable.
    '
    ' aList - The list to be padded
    ' aPadder - The value to pad with
    ' aLength - The length of the final list after padding
    '''
    return aList + [aPadder] * (aLength - len(aList))


def pad_output(anOutput, anEmptyOutput, anAlleleLen):
    '''
    ' This function pads some of the output components with null or zero
    ' values.  If a variant is found in a sample and the output for previous
    ' samples has already been formatted, it needs to be reformatted with null
    ' or zero values.  For example, all of the allele specific components such
    ' as depth and frequency need to be set to zero in previous samples.
    '
    ' anOutput - The formatted output for a sample
    ' anAlleleLen - The number of alleles found at this site
    '''

    # if there is no data, then just return
    if (anOutput == anEmptyOutput):
        return anOutput

    # get the data for this sample
    # GT:DP:AD:AF:INS:DEL:DP4:START:STOP:MQ0:MMQ:MQA:BQ:SB:MMP
    (genotypes, depths, alleleDepths, alleleFreqs, insCount, delCount,
     depths4, starts, stops, mapQualZeroes, mapQualMaxes, mapQuals,
     baseQuals, strandBiases, mmps) = anOutput.split(":")

    # if we need some padding
    alleleDepthList = alleleDepths.split(",")
    depths4List = depths4.split(",")
    if (len(alleleDepthList) < anAlleleLen):
        alleleDepthList = pad(alleleDepthList, "0", anAlleleLen)
        depths4List = pad(depths4List, "0", (anAlleleLen*2))
        alleleFreqsList = pad(alleleFreqs.split(","), "0.0", anAlleleLen)
        mapQualZeroesList = pad(mapQualZeroes.split(","), "0", anAlleleLen)
        mapQualMaxesList = pad(mapQualMaxes.split(","), "0", anAlleleLen)
        mapQualsList = pad(mapQuals.split(","), "0", anAlleleLen)
        baseQualityList = pad(baseQuals.split(","), "0", anAlleleLen)
        strandBiasList = pad(strandBiases.split(","), "0.0", anAlleleLen)
        mmpList = pad(mmps.split(","), ".", anAlleleLen)

        # GT:DP:AD:AF:INS:DEL:DP4:START:STOP:MQ0:MMQ:MQA:BQ:SB:MMP
        outputList = (genotypes, depths, ",".join(alleleDepthList),
                      ",".join(alleleFreqsList), insCount, delCount,
                      ",".join(depths4List), starts, stops,
                      ",".join(mapQualZeroesList), ",".join(mapQualMaxesList),
                      ",".join(mapQualsList), ",".join(baseQualityList),
                      ",".join(strandBiasList), ",".join(mmpList))

        return ":".join(outputList)
    else:
        # no padding necessary
        return anOutput


def main():

    '''
    # command for running this on a small test case:
    python radia.py TCGA-AB-2995 12
    --normalUseChr --tumorUseChr --rnaUseChr
    -n ../data/test/TCGA-AB-2995_normal.sam
    -t ../data/test/TCGA-AB-2995_tumor.sam
    -r ../data/test/TCGA-AB-2995_rna.sam

    # commands for running this on real data:
    python radia.py uniqueId X
    -n normalDna.bam
    -t tumorDna.bam
    -r tumorRna.bam
    -f all_sequences.fasta
    -o /path/to/output/uniqueId.vcf
    -e hg19
    -u https://url/to/fasta/hg19.fasta
    '''

    i_radiaVersion = "v1.1.4"
    i_vcfFormat = "VCFv4.1"

    # create the usage statement
    usage = "usage: python %prog id chrom [Options]"
    i_cmdLineParser = OptionParser(usage=usage, version=i_radiaVersion)

    # add the optional parameters
    i_cmdLineParser.add_option(
        "-b", "--batchSize", type="int",
        dest="batchSize", default=int(250000000), metavar="BATCH_SIZE",
        help="the size of the samtool selections that are loaded into " +
             "memory at one time, %default by default")
    i_cmdLineParser.add_option(
        "-o", "--outputFilename", default=sys.stdout,
        dest="outputFilename", metavar="OUTPUT_FILE",
        help="the name of the output file, append .gz if the " +
             "file should be gzipped, STDOUT by default")
    i_cmdLineParser.add_option(
        "-c", "--coordinatesFilename",
        dest="coordinatesFilename", metavar="COORDINATES_FILE",
        help="a tab-delimited file with 3 columns: (chr, start, stop) " +
             "specifying coordinates or coordinate ranges to query")
    i_cmdLineParser.add_option(
        "-f", "--fastaFilename",
        dest="fastaFilename", metavar="FASTA_FILE",
        help="the name of the fasta file that can be used on " +
             "all .bams, see below for specifying individual " +
             "fasta files for each .bam file")
    i_cmdLineParser.add_option(
        "-p", "--useChrPrefix", action="store_true",
        dest="useChrPrefix",  default=False,
        help="include this argument if the 'chr' prefix should be used " +
             "in the samtools command for all .bams, see below for " +
             "specifying the prefix for individual .bam files")
    i_cmdLineParser.add_option(
        "-l", "--log",
        dest="logLevel", default="WARNING", metavar="LOG",
        help="the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL), " +
             "%default by default")
    i_cmdLineParser.add_option(
        "-g", "--logFilename",
        dest="logFilename", metavar="LOG_FILE",
        help="the name of the log file, STDOUT by default")
    i_cmdLineParser.add_option(
        "-i", "--refId",
        dest="refId", metavar="REF_ID",
        help="the reference Id - used in the reference VCF meta tag")
    i_cmdLineParser.add_option(
        "-u", "--refUrl",
        dest="refUrl", metavar="REF_URL",
        help="the URL for the reference VCF meta tag")
    i_cmdLineParser.add_option(
        "-m", "--refFilename",
        dest="refFilename", metavar="REF_FILE",
        help="the location for the reference VCF meta tag")
    i_cmdLineParser.add_option(
        "-a", "--startCoordinate", type="int", default=int(1),
        dest="startCoordinate", metavar="START_COORDINATE",
        help="the start coordinate for testing small regions, " +
             "%default by default")
    i_cmdLineParser.add_option(
        "-z", "--stopCoordinate", type="int", default=int(0),
        dest="stopCoordinate", metavar="STOP_COORDINATE",
        help="the stop coordinate for testing small regions, " +
             "%default by default")
    i_cmdLineParser.add_option(
        "-d", "--dataSource",
        dest="dataSource", metavar="DATA_SOURCE",
        help="the source of the data - used in the sample VCF meta tag")
    i_cmdLineParser.add_option(
        "-q", "--sequencingPlatform",
        dest="sequencingPlatform", metavar="SEQ_PLATFORM",
        help="the sequencing platform - used in the sample VCF meta tag")
    i_cmdLineParser.add_option(
        "-s", "--statsDir",
        dest="statsDir", metavar="STATS_DIR",
        help="a stats directory where some basic stats can be output")
    i_cmdLineParser.add_option(
        "", "--disease",
        dest="disease", metavar="DISEASE",
        help="a disease abbreviation (i.e. BRCA) for the header")
    i_cmdLineParser.add_option(
        "", "--rnaIncludeSecondaryAlignments", action="store_true",
        dest="rnaIncludeSecondaryAlignments", default=False,
        help="if you align the RNA to transcript isoforms, " +
             "then you may want to include RNA secondary " +
             "alignments in the samtools mpileups")
    i_cmdLineParser.add_option(
        "", "--noHeader", action="store_false",
        dest="outputHeader", default=True,
        help="include this argument if the header should not be output")
    i_cmdLineParser.add_option(
        "", "--outputAllData", action="store_true",
        dest="outputAllData", default=False,
        help="include this argument if all data should be output " +
             "regardless of the existence of a variant")
    i_cmdLineParser.add_option(
        "", "--loadCoordinatesRange", action="store_true",
        dest="loadCoordinatesRange", default=False,
        help="include this argument to improve performance by "
             "loading the entire range in the coordinates file")
    # e,j,k,v,w,y

    i_cmdLineParser.add_option(
        "", "--genotypeMinDepth", type="int", default=int(2),
        dest="genotypeMinDepth", metavar="GT_MIN_DP",
        help="the minimum number of bases required for the genotype, " +
             "%default by default")
    i_cmdLineParser.add_option(
        "", "--genotypeMinPct", type="float", default=float(.10),
        dest="genotypeMinPct", metavar="GT_MIN_PCT",
        help="the minimum percentage of reads required for the genotype, " +
             "%default by default")

    # params for normal DNA
    i_cmdLineParser.add_option(
        "-n", "--dnaNormalFilename",
        dest="dnaNormalFilename", metavar="DNA_NORMAL_FILE",
        help="the name of the normal DNA .bam file")
    i_cmdLineParser.add_option(
        "--np", "--dnaNormalPileupsFilename",
        dest="dnaNormalPileupsFilename", metavar="DNA_NORMAL_PILEUPS",
        help="the name of the normal DNA mpileup file")
    i_cmdLineParser.add_option(
        "", "--dnaNormalMinTotalBases", type="int", default=int(4),
        dest="dnaNormalMinTotalNumBases", metavar="DNA_NORM_MIN_TOTAL_BASES",
        help="the minimum number of overall normal DNA reads " +
             "covering a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaNormalMinAltBases", type="int", default=int(2),
        dest="dnaNormalMinAltNumBases", metavar="DNA_NORM_MIN_ALT_BASES",
        help="the minimum number of alternative normal DNA reads " +
             "supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaNormalBaseQual", type="int", default=int(10),
        dest="dnaNormalMinBaseQuality", metavar="DNA_NORM_BASE_QUAL",
        help="the minimum normal DNA base quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaNormalMapQual", type="int", default=int(10),
        dest="dnaNormalMinMappingQuality", metavar="DNA_NORM_MAP_QUAL",
        help="the minimum normal DNA mapping quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaNormalUseChr", action="store_true", default=False,
        dest="dnaNormalUseChrPrefix",
        help="include this argument if the 'chr' prefix should be used " +
             "in the samtools command for the normal DNA .bam file")
    i_cmdLineParser.add_option(
        "", "--dnaNormalFasta",
        dest="dnaNormalFastaFilename", metavar="DNA_NORM_FASTA_FILE",
        help="the name of the fasta file for the normal DNA .bam file")
    i_cmdLineParser.add_option(
        "", "--dnaNormalMitochon", default="M",
        dest="dnaNormalMitochon", metavar="DNA_NORM_MITOCHON",
        help="the short name for the mitochondrial DNA (e.g 'M' or 'MT'), " +
             "%default by default")
    i_cmdLineParser.add_option(
        "", "--dnaNormalDescription", default="Normal DNA Sample",
        dest="dnaNormalDesc", metavar="DNA_NORM_DESC",
        help="the description for the sample in the VCF header, " +
             "%default by default")
    '''
    i_cmdLineParser.add_option(
        "", "--dnaNormalLabel", default="DNA_NORMAL",
        dest="dnaNormalLabel", metavar="DNA_NOR_LABEL",
        help="the column header for the sample in the VCF file, " +
             "%default by default")
    '''

    # params for normal RNA
    i_cmdLineParser.add_option(
        "-x", "--rnaNormalFilename",
        dest="rnaNormalFilename", metavar="RNA_NORMAL_FILE",
        help="the name of the normal RNA-Seq .bam file")
    i_cmdLineParser.add_option(
        "--xp", "--rnaNormalPileupsFilename",
        dest="rnaNormalPileupsFilename", metavar="RNA_NORMAL_PILEUPS",
        help="the name of the normal RNA-Seq mpileup file")
    i_cmdLineParser.add_option(
        "", "--rnaNormalMinTotalBases", type="int", default=int(4),
        dest="rnaNormalMinTotalNumBases", metavar="RNA_NORM_MIN_TOTAL_BASES",
        help="the minimum number of overall normal RNA-Seq reads " +
             "covering a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaNormalMinAltBases", type="int", default=int(2),
        dest="rnaNormalMinAltNumBases", metavar="RNA_NORM_MIN_ALT_BASES",
        help="the minimum number of alternative normal RNA-Seq reads " +
             "supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaNormalBaseQual", type="int", default=int(10),
        dest="rnaNormalMinBaseQuality", metavar="RNA_NORM_BASE_QUAL",
        help="the minimum normal RNA-Seq base quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaNormalMapQual", type="int", default=int(10),
        dest="rnaNormalMinMappingQuality", metavar="RNA_NORM_MAP_QUAL",
        help="the minimum normal RNA-Seq mapping quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaNormalUseChr", action="store_true", default=False,
        dest="rnaNormalUseChrPrefix",
        help="include this argument if the 'chr' prefix should be used " +
             "in the samtools command for the normal RNA .bam file")
    i_cmdLineParser.add_option(
        "", "--rnaNormalFasta",
        dest="rnaNormalFastaFilename", metavar="RNA_NORM_FASTA_FILE",
        help="the name of the fasta file for the normal RNA .bam file")
    i_cmdLineParser.add_option(
        "", "--rnaNormalMitochon", default="M",
        dest="rnaNormalMitochon", metavar="RNA_NORM_MITOCHON",
        help="the short name for the mitochondrial RNA (e.g 'M' or 'MT')," +
             "%default by default")
    i_cmdLineParser.add_option(
        "", "--rnaNormalDescription", default="Normal RNA Sample",
        dest="rnaNormalDesc", metavar="RNA_NORM_DESC",
        help="the description for the sample in the VCF header, " +
             "%default by default")
    '''
    i_cmdLineParser.add_option(
        "", "--rnaNormalLabel", default="RNA_NORMAL",
        dest="rnaNormalLabel", metavar="RNA_NOR_LABEL",
        help="the column header for the sample in the VCF file, " +
             "%default by default")
    '''

    # params for tumor DNA
    i_cmdLineParser.add_option(
        "-t", "--dnaTumorFilename",
        dest="dnaTumorFilename", metavar="DNA_TUMOR_FILE",
        help="the name of the tumor DNA .bam file")
    i_cmdLineParser.add_option(
        "--tp", "--dnaTumorPileupsFilename",
        dest="dnaTumorPileupsFilename", metavar="DNA_TUMOR_PILEUPS",
        help="the name of the tumor DNA mpileup file")
    i_cmdLineParser.add_option(
        "", "--dnaTumorMinTotalBases", type="int", default=int(4),
        dest="dnaTumorMinTotalNumBases", metavar="DNA_TUM_MIN_TOTAL_BASES",
        help="the minimum number of overall tumor DNA reads " +
             "covering a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaTumorMinAltBases", type="int", default=int(2),
        dest="dnaTumorMinAltNumBases", metavar="DNA_TUM_MIN_ALT_BASES",
        help="the minimum number of alternative tumor DNA reads " +
             "supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaTumorBaseQual", type="int", default=int(10),
        dest="dnaTumorMinBaseQuality", metavar="DNA_TUM_BASE_QUAL",
        help="the minimum tumor DNA base quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaTumorMapQual", type="int", default=int(10),
        dest="dnaTumorMinMappingQuality", metavar="DNA_TUM_MAP_QUAL",
        help="the minimum tumor DNA mapping quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--dnaTumorUseChr", action="store_true", default=False,
        dest="dnaTumorUseChrPrefix",
        help="include this argument if the 'chr' prefix should be used " +
             "in the samtools command for the tumor DNA .bam file")
    i_cmdLineParser.add_option(
        "", "--dnaTumorFasta",
        dest="dnaTumorFastaFilename", metavar="DNA_TUM_FASTA_FILE",
        help="the name of the fasta file for the tumor DNA .bam file")
    i_cmdLineParser.add_option(
        "", "--dnaTumorMitochon", default="M",
        dest="dnaTumorMitochon", metavar="DNA_TUM_MITOCHON",
        help="the short name for the mitochondrial DNA (e.g 'M' or 'MT'), " +
             "%default by default")
    i_cmdLineParser.add_option(
        "", "--dnaTumorDescription", default="Tumor DNA Sample",
        dest="dnaTumorDesc", metavar="DNA_TUM_DESC",
        help="the description for the sample in the VCF header, " +
             "%default by default")
    '''
    i_cmdLineParser.add_option(
        "", "--dnaTumorLabel", default="DNA_TUMOR",
        dest="dnaTumorLabel", metavar="DNA_TUM_LABEL",
        help="the column header for the sample in the VCF file, " +
             "%default by default")
    '''

    # params for tumor RNA
    i_cmdLineParser.add_option(
        "-r", "--rnaTumorFilename",
        dest="rnaTumorFilename", metavar="RNA_TUMOR_FILE",
        help="the name of the tumor RNA-Seq .bam file")
    i_cmdLineParser.add_option(
        "--rp", "--rnaTumorPileupsFilename",
        dest="rnaTumorPileupsFilename", metavar="RNA_TUMOR_PILEUPS",
        help="the name of the tumor RNA-Seq mpileup file")
    i_cmdLineParser.add_option(
        "", "--rnaTumorMinTotalBases", type="int", default=int(4),
        dest="rnaTumorMinTotalNumBases", metavar="RNA_TUM_MIN_TOTAL_BASES",
        help="the minimum number of overall tumor RNA-Seq reads " +
             "covering a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaTumorMinAltBases", type="int", default=int(2),
        dest="rnaTumorMinAltNumBases", metavar="RNA_TUM_MIN_ALT_BASES",
        help="the minimum number of alternative tumor RNA-Seq reads " +
             "supporting a variant at a position, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaTumorBaseQual", type="int", default=int(10),
        dest="rnaTumorMinBaseQuality", metavar="RNA_TUM_BASE_QUAL",
        help="the minimum tumor RNA-Seq base quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaTumorMapQual", type="int", default=int(10),
        dest="rnaTumorMinMappingQuality", metavar="RNA_TUM_MAP_QUAL",
        help="the minimum tumor RNA-Seq mapping quality, %default by default")
    i_cmdLineParser.add_option(
        "", "--rnaTumorUseChr", action="store_true", default=False,
        dest="rnaTumorUseChrPrefix",
        help="include this argument if the 'chr' prefix should be used " +
             "in the samtools command for the tumor RNA .bam file")
    i_cmdLineParser.add_option(
        "", "--rnaTumorFasta",
        dest="rnaTumorFastaFilename", metavar="RNA_TUM_FASTA_FILE",
        help="the name of the fasta file for the tumor RNA .bam file")
    i_cmdLineParser.add_option(
        "", "--rnaTumorMitochon", default="M",
        dest="rnaTumorMitochon", metavar="RNA_TUM_MITOCHON",
        help="the short name for the mitochondrial RNA (e.g 'M' or 'MT'), " +
             "%default by default")
    i_cmdLineParser.add_option(
        "", "--rnaTumorDescription", default="Tumor RNA Sample",
        dest="rnaTumorDesc", metavar="RNA_TUM_DESC",
        help="the description for the sample in the VCF header, " +
             "%default by default")
    '''
    i_cmdLineParser.add_option(
        "", "--rnaTumorLabel", default="RNA_TUMOR",
        dest="rnaTumorLabel", metavar="RNA_TUM_LABEL",
        help="the column header for the sample in the VCF file, " +
             "%default by default")
    '''

    # first parse the args
    (cmdLineOpts, cmdLineArgs) = i_cmdLineParser.parse_args()

    # range(inclusiveFrom, exclusiveTo, by)
    i_possibleArgLengths = range(3, 80, 1)
    i_argLength = len(sys.argv)

    # check if this is one of the possible correct commands
    if (i_argLength not in i_possibleArgLengths):
        i_cmdLineParser.print_help()
        sys.exit(0)

    # get the required params
    cmdLineOptsDict = vars(cmdLineOpts)
    i_id = str(cmdLineArgs[0])
    i_chrom = str(cmdLineArgs[1])

    # get the optional params with default values
    i_batchSize = cmdLineOpts.batchSize
    i_useChrPrefix = cmdLineOpts.useChrPrefix
    i_rnaIncludeSecAlignments = cmdLineOpts.rnaIncludeSecondaryAlignments
    i_logLevel = cmdLineOpts.logLevel
    i_startCoordinate = cmdLineOpts.startCoordinate
    i_stopCoordinate = cmdLineOpts.stopCoordinate
    i_refId = cmdLineOpts.refId
    i_refUrl = cmdLineOpts.refUrl
    i_outputHeader = cmdLineOpts.outputHeader
    i_outputAllData = cmdLineOpts.outputAllData
    i_loadCoordinatesRange = cmdLineOpts.loadCoordinatesRange

    i_genotypeMinDepth = cmdLineOpts.genotypeMinDepth
    i_genotypeMinPct = cmdLineOpts.genotypeMinPct

    i_dnaNormMinTotalBases = cmdLineOpts.dnaNormalMinTotalNumBases
    i_dnaNormMinAltBases = cmdLineOpts.dnaNormalMinAltNumBases
    i_dnaNormMinBaseQual = cmdLineOpts.dnaNormalMinBaseQuality
    i_dnaNormMinMapQual = cmdLineOpts.dnaNormalMinMappingQuality
    i_dnaNormUseChr = cmdLineOpts.dnaNormalUseChrPrefix
    i_dnaNormMitochon = cmdLineOpts.dnaNormalMitochon
    i_dnaNormDesc = cmdLineOpts.dnaNormalDesc
    # i_dnaNormLabel = cmdLineOpts.dnaNormalLabel
    i_dnaNormLabel = "DNA_NORMAL"

    i_rnaNormMinTotalBases = cmdLineOpts.rnaNormalMinTotalNumBases
    i_rnaNormMinAltBases = cmdLineOpts.rnaNormalMinAltNumBases
    i_rnaNormMinBaseQual = cmdLineOpts.rnaNormalMinBaseQuality
    i_rnaNormMinMapQual = cmdLineOpts.rnaNormalMinMappingQuality
    i_rnaNormUseChr = cmdLineOpts.rnaNormalUseChrPrefix
    i_rnaNormMitochon = cmdLineOpts.rnaNormalMitochon
    i_rnaNormDesc = cmdLineOpts.rnaNormalDesc
    # i_rnaNormLabel = cmdLineOpts.rnaNormalLabel
    i_rnaNormLabel = "RNA_NORMAL"

    i_dnaTumMinTotalBases = cmdLineOpts.dnaTumorMinTotalNumBases
    i_dnaTumMinAltBases = cmdLineOpts.dnaTumorMinAltNumBases
    i_dnaTumMinBaseQual = cmdLineOpts.dnaTumorMinBaseQuality
    i_dnaTumMinMapQual = cmdLineOpts.dnaTumorMinMappingQuality
    i_dnaTumUseChr = cmdLineOpts.dnaTumorUseChrPrefix
    i_dnaTumMitochon = cmdLineOpts.dnaTumorMitochon
    i_dnaTumDesc = cmdLineOpts.dnaTumorDesc
    # i_dnaTumLabel = cmdLineOpts.dnaTumorLabel
    i_dnaTumLabel = "DNA_TUMOR"

    i_rnaTumMinTotalBases = cmdLineOpts.rnaTumorMinTotalNumBases
    i_rnaTumMinAltBases = cmdLineOpts.rnaTumorMinAltNumBases
    i_rnaTumMinBaseQual = cmdLineOpts.rnaTumorMinBaseQuality
    i_rnaTumMinMapQual = cmdLineOpts.rnaTumorMinMappingQuality
    i_rnaTumUseChr = cmdLineOpts.rnaTumorUseChrPrefix
    i_rnaTumMitochon = cmdLineOpts.rnaTumorMitochon
    i_rnaTumDesc = cmdLineOpts.rnaTumorDesc
    # i_rnaTumLabel = cmdLineOpts.rnaTumorLabel
    i_rnaTumLabel = "RNA_TUMOR"

    # the user can specify a universal prefix to be used on all bams
    if (i_useChrPrefix):
        i_dnaNormUseChr = True
        i_dnaTumUseChr = True
        i_rnaNormUseChr = True
        i_rnaTumUseChr = True

    # try to get any optional parameters with no defaults
    i_readFilenameList = []
    i_writeFilenameList = []
    i_dirList = []
    filenames = []
    labels = []
    descriptions = []
    analytes = []

    i_outputFilename = None
    i_logFilename = None
    i_dnaNormalFilename = None
    i_dnaNormalPileupsFilename = None
    i_dnaNormalGenerator = None
    i_dnaTumorFilename = None
    i_dnaTumorPileupsFilename = None
    i_dnaTumorGenerator = None
    i_rnaNormalFilename = None
    i_rnaNormalPileupsFilename = None
    i_rnaNormalGenerator = None
    i_rnaTumorFilename = None
    i_rnaTumorPileupsFilename = None
    i_rnaTumorGenerator = None
    i_dnaNormalFastaFilename = None
    i_dnaTumorFastaFilename = None
    i_rnaNormalFastaFilename = None
    i_rnaTumorFastaFilename = None
    i_coordinatesFilename = None
    i_universalFastaFilename = None
    i_refFilename = None
    i_statsDir = None
    i_dataSource = None
    i_sequencingPlatform = None
    i_disease = None

    if (cmdLineOpts.dnaNormalPileupsFilename is not None):
        i_dnaNormalPileupsFilename = str(cmdLineOpts.dnaNormalPileupsFilename)
        i_readFilenameList += [i_dnaNormalPileupsFilename]
    if (cmdLineOpts.rnaNormalPileupsFilename is not None):
        i_rnaNormalPileupsFilename = str(cmdLineOpts.rnaNormalPileupsFilename)
        i_readFilenameList += [i_rnaNormalPileupsFilename]
    if (cmdLineOpts.dnaTumorPileupsFilename is not None):
        i_dnaTumorPileupsFilename = str(cmdLineOpts.dnaTumorPileupsFilename)
        i_readFilenameList += [i_dnaTumorPileupsFilename]
    if (cmdLineOpts.rnaTumorPileupsFilename is not None):
        i_rnaTumorPileupsFilename = str(cmdLineOpts.rnaTumorPileupsFilename)
        i_readFilenameList += [i_rnaTumorPileupsFilename]

    if (cmdLineOpts.dnaNormalFilename is not None):
        i_dnaNormalFilename = str(cmdLineOpts.dnaNormalFilename)
        i_readFilenameList += [i_dnaNormalFilename]
        filenames += [i_dnaNormalFilename]
        labels += [i_dnaNormLabel]
        descriptions += [i_dnaNormDesc]
        analytes += ["DNA"]
    if (cmdLineOpts.rnaNormalFilename is not None):
        i_rnaNormalFilename = str(cmdLineOpts.rnaNormalFilename)
        i_readFilenameList += [i_rnaNormalFilename]
        filenames += [i_rnaNormalFilename]
        labels += [i_rnaNormLabel]
        descriptions += [i_rnaNormDesc]
        analytes += ["RNA"]
    if (cmdLineOpts.dnaTumorFilename is not None):
        i_dnaTumorFilename = str(cmdLineOpts.dnaTumorFilename)
        i_readFilenameList += [i_dnaTumorFilename]
        filenames += [i_dnaTumorFilename]
        labels += [i_dnaTumLabel]
        descriptions += [i_dnaTumDesc]
        analytes += ["DNA"]
    if (cmdLineOpts.rnaTumorFilename is not None):
        i_rnaTumorFilename = str(cmdLineOpts.rnaTumorFilename)
        i_readFilenameList += [i_rnaTumorFilename]
        filenames += [i_rnaTumorFilename]
        labels += [i_rnaTumLabel]
        descriptions += [i_rnaTumDesc]
        analytes += ["RNA"]
    if (cmdLineOpts.outputFilename is not None):
        i_outputFilename = cmdLineOpts.outputFilename
        if (cmdLineOpts.outputFilename is not sys.stdout):
            i_writeFilenameList += [i_outputFilename]
    if (cmdLineOpts.logFilename is not None):
        i_logFilename = str(cmdLineOpts.logFilename)
        i_writeFilenameList += [i_logFilename]
    if (cmdLineOpts.coordinatesFilename is not None):
        i_coordinatesFilename = str(cmdLineOpts.coordinatesFilename)
        i_readFilenameList += [i_coordinatesFilename]
    if (cmdLineOpts.refFilename is not None):
        i_refFilename = str(cmdLineOpts.refFilename)
    if (cmdLineOpts.statsDir is not None):
        i_statsDir = str(cmdLineOpts.statsDir)
        i_dirList += [i_statsDir]
    if (cmdLineOpts.dataSource is not None):
        i_dataSource = str(cmdLineOpts.dataSource)
    if (cmdLineOpts.sequencingPlatform is not None):
        i_sequencingPlatform = str(cmdLineOpts.sequencingPlatform)
    if (cmdLineOpts.disease is not None):
        i_disease = str(cmdLineOpts.disease)

    # if a universal fasta file is specified, then use it
    if (cmdLineOpts.fastaFilename is not None):
        i_universalFastaFilename = str(cmdLineOpts.fastaFilename)
        i_dnaNormalFastaFilename = i_universalFastaFilename
        i_dnaTumorFastaFilename = i_universalFastaFilename
        i_rnaNormalFastaFilename = i_universalFastaFilename
        i_rnaTumorFastaFilename = i_universalFastaFilename

    # if individual fasta files are specified, they over-ride the universal one
    if (cmdLineOpts.dnaNormalFastaFilename is not None):
        i_dnaNormalFastaFilename = str(cmdLineOpts.dnaNormalFastaFilename)
        i_readFilenameList += [i_dnaNormalFastaFilename]
        if (i_universalFastaFilename is None):
            i_universalFastaFilename = i_dnaNormalFastaFilename
    if (cmdLineOpts.rnaNormalFastaFilename is not None):
        i_rnaNormalFastaFilename = str(cmdLineOpts.rnaNormalFastaFilename)
        i_readFilenameList += [i_rnaNormalFastaFilename]
        if (i_universalFastaFilename is None):
            i_universalFastaFilename = i_rnaNormalFastaFilename
    if (cmdLineOpts.dnaTumorFastaFilename is not None):
        i_dnaTumorFastaFilename = str(cmdLineOpts.dnaTumorFastaFilename)
        i_readFilenameList += [i_dnaTumorFastaFilename]
        if (i_universalFastaFilename is None):
            i_universalFastaFilename = i_dnaTumorFastaFilename
    if (cmdLineOpts.rnaTumorFastaFilename is not None):
        i_rnaTumorFastaFilename = str(cmdLineOpts.rnaTumorFastaFilename)
        i_readFilenameList += [i_rnaTumorFastaFilename]
        if (i_universalFastaFilename is None):
            i_universalFastaFilename = i_rnaTumorFastaFilename
    if (i_universalFastaFilename is not None):
        i_readFilenameList += [i_universalFastaFilename]

    # need to set these for the vcf header,
    # especially when only a universal fasta file is specified
    cmdLineOptsDict["dnaNormalFastaFilename"] = i_dnaNormalFastaFilename
    cmdLineOptsDict["dnaTumorFastaFilename"] = i_dnaTumorFastaFilename
    cmdLineOptsDict["rnaNormalFastaFilename"] = i_rnaNormalFastaFilename
    cmdLineOptsDict["rnaTumorFastaFilename"] = i_rnaTumorFastaFilename

    # assuming loglevel is bound to the string value obtained from the
    # command line argument. Convert to upper case to allow the user to
    # specify --log=DEBUG or --log=debug
    i_numericLogLevel = getattr(logging, i_logLevel.upper(), None)
    if not isinstance(i_numericLogLevel, int):
        raise ValueError("Invalid log level: '%s' must be one of the " +
                         "following:  DEBUG, INFO, WARNING, ERROR, CRITICAL",
                         i_logLevel)

    # set up the logging
    if (i_logFilename is not None):
        logging.basicConfig(
            level=i_numericLogLevel,
            filename=i_logFilename,
            filemode='w',
            format='%(asctime)s\t%(levelname)s\t%(message)s',
            datefmt='%m/%d/%Y %I:%M:%S %p')
    else:
        logging.basicConfig(
            level=i_numericLogLevel,
            format='%(asctime)s\t%(levelname)s\t%(message)s',
            datefmt='%m/%d/%Y %I:%M:%S %p')

    # set the debug
    i_debug = (i_numericLogLevel == logging.DEBUG)

    # output some debug info
    if (i_debug):
        logging.debug("id=%s" % i_id)
        logging.debug("chrom=%s" % i_chrom)
        logging.debug("outputFilename=%s" % i_outputFilename)
        logging.debug("logLevel=%s" % i_logLevel)
        logging.debug("logFilename=%s" % i_logFilename)
        logging.debug("batchSize=%s" % i_batchSize)
        logging.debug("coordinatesFile=%s" % i_coordinatesFilename)
        logging.debug("vcfFormat=%s" % i_vcfFormat)
        logging.debug("startCoordinate=%s" % i_startCoordinate)
        logging.debug("stopCoordinate=%s" % i_stopCoordinate)
        logging.debug("refId=%s" % i_refId)
        logging.debug("refUrl=%s" % i_refUrl)
        logging.debug("disease=%s" % i_disease)
        logging.debug("refFilename=%s" % i_refFilename)
        logging.debug("statsDir=%s" % i_statsDir)
        logging.debug("rnaInclSecAlign=%s" % i_rnaIncludeSecAlignments)
        logging.debug("outputHeader=%s" % i_outputHeader)
        logging.debug("outputAllData=%s" % i_outputAllData)
        logging.debug("loadCoordinatesRange=%s" % i_loadCoordinatesRange)

        logging.debug("genotypeMinDepth=%s" % i_genotypeMinDepth)
        logging.debug("genotypeMinPct=%s" % i_genotypeMinPct)

        if (i_dnaNormalFilename is not None):
            logging.debug("dnaNormal=%s" % i_dnaNormalFilename)
        if (i_dnaNormalPileupsFilename is not None):
            logging.debug("dnaNormal=%s" % i_dnaNormalPileupsFilename)
        logging.debug("dna normal fasta File: %s" % i_dnaNormalFastaFilename)
        logging.debug("dna normal minBaseQual: %s" % i_dnaNormMinBaseQual)
        logging.debug("dna normal minMappingQual: %s" % i_dnaNormMinMapQual)
        logging.debug("dna normal minTotal: %s" % i_dnaNormMinTotalBases)
        logging.debug("dna normal minAltBases: %s" % i_dnaNormMinAltBases)
        logging.debug("dna normal usePrefix? %s" % i_dnaNormUseChr)
        logging.debug("dna normal mitochon %s" % i_dnaNormMitochon)

        if (i_dnaTumorFilename is not None):
            logging.debug("dnaTumor=%s" % i_dnaTumorFilename)
        if (i_dnaTumorPileupsFilename is not None):
            logging.debug("dnaTumor=%s" % i_dnaTumorPileupsFilename)
        logging.debug("dna tumor fasta File: %s" % i_dnaTumorFastaFilename)
        logging.debug("dna tumor minBaseQual: %s" % i_dnaTumMinBaseQual)
        logging.debug("dna tumor minMappingQual: %s" % i_dnaTumMinMapQual)
        logging.debug("dna tumor minTotal: %s" % i_dnaTumMinTotalBases)
        logging.debug("dna tumor minAltBases: %s" % i_dnaTumMinAltBases)
        logging.debug("dna tumor usePrefix? %s" % i_dnaTumUseChr)
        logging.debug("dna tumor mitochon %s" % i_dnaTumMitochon)

        if (i_rnaNormalFilename is not None):
            logging.debug("rnaNormal=%s" % i_rnaNormalFilename)
        if (i_rnaNormalPileupsFilename is not None):
            logging.debug("rnaNormal=%s" % i_rnaNormalPileupsFilename)
        logging.debug("rna normal fasta File: %s" % i_rnaNormalFastaFilename)
        logging.debug("rna normal minBaseQual: %s" % i_rnaNormMinBaseQual)
        logging.debug("rna normal minMappingQual: %s" % i_rnaNormMinMapQual)
        logging.debug("rna normal minTotal: %s" % i_rnaNormMinTotalBases)
        logging.debug("rna normal minAltBases: %s" % i_rnaNormMinAltBases)
        logging.debug("rna normal usePrefix? %s" % i_rnaNormUseChr)
        logging.debug("rna normal mitochon %s" % i_rnaNormMitochon)

        if (i_rnaTumorFilename is not None):
            logging.debug("rnaTumor=%s" % i_rnaTumorFilename)
        if (i_rnaTumorPileupsFilename is not None):
            logging.debug("rnaTumor=%s" % i_rnaTumorPileupsFilename)
        logging.debug("rna tumor fasta File: %s" % i_rnaTumorFastaFilename)
        logging.debug("rna tumor minBaseQual: %s" % i_rnaTumMinBaseQual)
        logging.debug("rna tumor minMappingQual: %s" % i_rnaTumMinMapQual)
        logging.debug("rna tumor minTotal: %s" % i_rnaTumMinTotalBases)
        logging.debug("rna tumor minAltBases: %s" % i_rnaTumMinAltBases)
        logging.debug("rna tumor usePrefix? %s" % i_rnaTumUseChr)
        logging.debug("rna tumor mitochon %s" % i_rnaTumMitochon)

    # check for any errors
    if (not radiaUtil.check_for_argv_errors(i_dirList,
                                            i_readFilenameList,
                                            i_writeFilenameList)):
        sys.exit(1)

    # the user must specify at least one .bam file
    if (i_dnaNormalFilename is None and
        i_dnaTumorFilename is None and
        i_rnaNormalFilename is None and
        i_rnaTumorFilename is None and
        i_dnaNormalPileupsFilename is None and
        i_dnaTumorPileupsFilename is None and
        i_rnaNormalPileupsFilename is None and
        i_rnaTumorPileupsFilename is None):
        logging.critical("You must specify at least one BAM file.")
        sys.exit(1)
    if (i_dnaNormalFilename is None and
        i_dnaNormalPileupsFilename is not None):
        logging.critical("You have specified a pileups file for the DNA " +
                         "normal sample, but the original .bam file is " +
                         "needed for filtering. Please specify both a .bam " +
                         "and a pileups file for the DNA normal sample.")
        sys.exit(1)
    if (i_rnaNormalFilename is None and
        i_rnaNormalPileupsFilename is not None):
        logging.critical("You have specified a pileups file for the RNA " +
                         "normal sample, but the original .bam file is " +
                         "needed for filtering. Please specify both a .bam " +
                         "and a pileups file for the RNA normal sample.")
        sys.exit(1)
    if (i_dnaTumorFilename is None and
        i_dnaTumorPileupsFilename is not None):
        logging.critical("You have specified a pileups file for the DNA " +
                         "tumor sample, but the original .bam file is " +
                         "needed for filtering. Please specify both a .bam " +
                         "and a pileups file for the DNA tumor sample.")
        sys.exit(1)
    if (i_rnaTumorFilename is None and
        i_rnaTumorPileupsFilename is not None):
        logging.critical("You have specified a pileups file for the RNA " +
                         "tumor sample, but the original .bam file is " +
                         "needed for filtering. Please specify both a .bam " +
                         "and a pileups file for the RNA tumor sample.")
        sys.exit(1)
    if (i_dnaNormalFilename is not None and
        not os.path.isfile(i_dnaNormalFilename + ".bai")):
        logging.critical("The index file for the BAM file " +
                         i_dnaNormalFilename + " doesn't exist. Please use " +
                         "the 'samtools index' command to create one.")
        sys.exit(1)
    if (i_rnaNormalFilename is not None and
        not os.path.isfile(i_rnaNormalFilename + ".bai")):
        logging.critical("The index file for the BAM file " +
                         i_rnaNormalFilename + " doesn't exist. Please use " +
                         "the 'samtools index' command to create one.")
        sys.exit(1)
    if (i_dnaTumorFilename is not None and
        not os.path.isfile(i_dnaTumorFilename + ".bai")):
        logging.critical("The index file for the BAM file " +
                         i_dnaTumorFilename + " doesn't exist. Please use " +
                         "the 'samtools index' command to create one.")
        sys.exit(1)
    if (i_rnaTumorFilename is not None and
        not os.path.isfile(i_rnaTumorFilename + ".bai")):
        logging.critical("The index file for the BAM file " +
                         i_rnaTumorFilename + " doesn't exist. Please use " +
                         "the 'samtools index' command to create one.")
        sys.exit(1)

    # the user cannot specify both a coordinates file and a pileups file
    if ((i_coordinatesFilename is not None and
         i_dnaNormalPileupsFilename is not None) or
        (i_coordinatesFilename is not None and
         i_rnaNormalPileupsFilename is not None) or
        (i_coordinatesFilename is not None and
         i_dnaTumorPileupsFilename is not None) or
        (i_coordinatesFilename is not None and
         i_rnaTumorPileupsFilename is not None)):
        logging.critical("You cannot specify a coordinates file with " +
                         "coordinate ranges to query and a pileups file " +
                         "with coordinates already queried. Please remove " +
                         "one or the other.")
        sys.exit(1)

    # make sure the user specified the necessary files
    if ((i_dnaNormalFilename is not None and
         i_dnaNormalFastaFilename is None) or
        (i_dnaTumorFilename is not None and
         i_dnaTumorFastaFilename is None) or
        (i_rnaNormalFilename is not None and
         i_rnaNormalFastaFilename is None) or
        (i_rnaTumorFilename is not None and
         i_rnaTumorFastaFilename is None)):
        logging.critical("You must specify the appropriate " +
                         "FASTA files when running RADIA.")
        sys.exit(1)
    if (i_dnaNormalFilename is not None and
        i_dnaNormalFastaFilename is not None and
        not os.path.isfile(i_dnaNormalFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " +
                         i_dnaNormalFastaFilename + " doesn't exist. Please " +
                         "use the 'samtools faidx' command to create one.")
        sys.exit(1)
    if (i_rnaNormalFilename is not None and
        i_rnaNormalFastaFilename is not None and
        not os.path.isfile(i_rnaNormalFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " +
                         i_rnaNormalFastaFilename + " doesn't exist. Please " +
                         "use the 'samtools faidx' command to create one.")
        sys.exit(1)
    if (i_dnaTumorFilename is not None and
        i_dnaTumorFastaFilename is not None and
        not os.path.isfile(i_dnaTumorFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " +
                         i_dnaTumorFastaFilename + " doesn't exist. Please " +
                         "use the 'samtools faidx' command to create one.")
        sys.exit(1)
    if (i_rnaTumorFilename is not None and
        i_rnaTumorFastaFilename is not None and
        not os.path.isfile(i_rnaTumorFastaFilename + ".fai")):
        logging.critical("The index file for the FASTA file " +
                         i_rnaTumorFastaFilename + " doesn't exist. Please " +
                         "use the 'samtools faidx' command to create one.")
        sys.exit(1)

    # get the stop coordinate if it hasn't been specified
    if (i_stopCoordinate == 0):
        if (i_universalFastaFilename is None):
            logging.critical("You must specify the appropriate " +
                             "FASTA files when running RADIA.")
            sys.exit(1)
        elif (i_universalFastaFilename is not None and
              not os.path.isfile(i_universalFastaFilename + ".fai")):
            logging.critical("The index file for the FASTA file " +
                             i_universalFastaFilename + " doesn't exist. " +
                             "Please use the 'samtools faidx' command " +
                             "to create one.")
            sys.exit(1)
        # if the coordinates file is not specified, then get the
        # stop coordinate for the chrom from the fasta index file
        elif (i_coordinatesFilename is None):
            i_chrSizeFileHandler = open(i_universalFastaFilename + ".fai", "r")
            i_stopCoordinate = get_chrom_size(i_chrom,
                                              i_chrSizeFileHandler,
                                              i_debug)
            i_chrSizeFileHandler.close()

            # catch some errors on the selection coordinates
            if (i_stopCoordinate == -1):
                logging.critical("Couldn't find chromosome '%s' in the " +
                                 "FASTA file that was specified.", i_chrom)
                sys.exit(1)

    # create lists of overall chroms, starts, and stops
    i_lookupChroms = list()
    i_lookupStarts = list()
    i_lookupStops = list()
    i_chroms = list()
    i_starts = list()
    i_stops = list()
    # when a coordinates file is provided:
    #    - if we should load the coordinate range:
    #        - set the overall range to be loaded
    #        - store the individual coordinate chroms, starts, and stops
    #    - else:
    #        - set all of the individual chroms, starts, and stops to be loaded
    if (i_coordinatesFilename is not None):
        coordinatesFh = radiaUtil.get_read_fileHandler(i_coordinatesFilename)
        if (i_loadCoordinatesRange):
            rangeDict = dict()
            for line in coordinatesFh:
                # if it is an empty line or header line, then just continue
                if (line.isspace() or line.startswith("#")):
                    continue
                # strip the carriage return and newline characters
                line = line.rstrip("\r\n")
                # split the line on the tab
                splitLine = line.split("\t")
                chrom = splitLine[0]
                start = int(splitLine[1])
                stop = int(splitLine[2])
                # store the individual chroms, starts, and stops
                i_lookupChroms.append(chrom)
                i_lookupStarts.append(start)
                i_lookupStops.append(stop)
                # find the first start and last stop per chrom
                # ideally, the coordinates file would be sorted
                # and only contain coordinates from one chrom
                # but we can't be sure of that
                if (chrom not in rangeDict):
                    rangeDict[chrom] = dict()
                    rangeDict[chrom]["start"] = start
                    rangeDict[chrom]["stop"] = stop
                else:
                    if start < rangeDict[chrom]["start"]:
                        rangeDict[chrom]["start"] = start
                    if stop > rangeDict[chrom]["stop"]:
                        rangeDict[chrom]["stop"] = stop
            # set the overall ranges to be loaded
            for chrom in rangeDict.iterkeys():
                i_chroms.append(chrom)
                i_starts.append(rangeDict[chrom]["start"])
                i_stops.append(rangeDict[chrom]["stop"])
        else:
            for line in coordinatesFh:
                # if it is an empty line or header line, then just continue
                if (line.isspace() or line.startswith("#")):
                    continue
                # strip the carriage return and newline characters
                line = line.rstrip("\r\n")
                # split the line on the tab
                splitLine = line.split("\t")
                # the coordinate is the second element
                i_chroms.append(splitLine[0])
                i_starts.append(int(splitLine[1]))
                i_stops.append(int(splitLine[2]))
        coordinatesFh.close()
    # otherwise, we have just one chrom, start, stop
    #    - the user either specified a start and stop with the -a and -z params
    #    - or they want the whole chromosome by specifying the chrom param
    else:
        i_chroms.append(i_chrom)
        i_starts.append(i_startCoordinate)
        i_stops.append(i_stopCoordinate)

    # EGFR chr7:55,248,979-55,259,567
    # i_startCoordinate = 55248979
    # i_stopCoordinate =  55249079
    # i_batchSize = 5

    # open the output stream
    if i_outputFilename is not sys.stdout:
        i_outputFileHandler = radiaUtil.get_write_fileHandler(i_outputFilename)
    else:
        i_outputFileHandler = i_outputFilename

    # if we should output the header
    if (i_outputHeader):

        # create the VCF header
        platforms = [i_sequencingPlatform] * len(filenames)
        sources = [i_dataSource] * len(filenames)
        # we don't want the start and stop coordinates in the header
        del cmdLineOptsDict["startCoordinate"]
        del cmdLineOptsDict["stopCoordinate"]
        output_vcf_header(i_outputFileHandler, i_vcfFormat, i_refId, i_refUrl,
                          i_refFilename, i_universalFastaFilename,
                          i_radiaVersion, i_id, cmdLineOptsDict, filenames,
                          labels, descriptions, platforms, sources,
                          analytes, i_disease)

    startTime = time.time()

    # initialize some variables
    formatString = "GT:DP:AD:AF:INS:DEL:DP4:START:STOP:MQ0:MMQ:MQA:BQ:SB:MMP"
    countRnaDnaCoordinateOverlap = 0
    totalGerms = 0
    totalSoms = 0
    totalNormEdits = 0
    totalTumEdits = 0
    totalNoRef = 0
    totalLohs = 0
    totalNormNotExp = 0
    totalTumNotExp = 0
    countRefMismatches = 0
    dnaSet = set()
    altList = list()
    refList = list()
    filterList = list()
    altCountsDict = collections.defaultdict(int)
    infoDict = collections.defaultdict(list)

    dnaNormalReadDPDict = collections.defaultdict(int)
    rnaNormalReadDPDict = collections.defaultdict(int)
    dnaTumorReadDPDict = collections.defaultdict(int)
    rnaTumorReadDPDict = collections.defaultdict(int)

    dnaNormalAltPercentDict = collections.defaultdict(int)
    rnaNormalAltPercentDict = collections.defaultdict(int)
    dnaTumorAltPercentDict = collections.defaultdict(int)
    rnaTumorAltPercentDict = collections.defaultdict(int)

    dnaNormalCoordWithData = 0
    dnaTumorCoordWithData = 0
    rnaNormalCoordWithData = 0
    rnaTumorCoordWithData = 0

    # this only needs to be initialized once for the first pass through
    # filterVariants(). filterVariants() creates a new baseCounts dict for
    # each sample and returns it. these are used for 2 purposes:
    # 1) to make a call when the previous sample didn't have enough bases
    #    for a call (not > aMinAltNumBases)
    # 2) to determine the bases that were lost on an LOH call
    previousBaseCounts = collections.defaultdict(int)
    dnaNormalPrevBaseCounts = collections.defaultdict(int)

    # for each chrom, start, and stop
    for (currentChrom, currentStart, currentStop) in izip(i_chroms,
                                                          i_starts,
                                                          i_stops):

        # error checking
        if (currentStart > currentStop):
            logging.critical("The start coordinate must be less than or " +
                             "equal to the stop coordinate %s:%s-%s",
                             currentChrom, currentStart, currentStop)
            sys.exit(1)

        if (i_debug):
            logging.debug("processing currentChrom=%s, currentStart=%s, " +
                          "currentStop=%s, i_batchSize=%s",
                          currentChrom, currentStart, currentStop, i_batchSize)

        # get the generators that will yield the pileups
        # the order matters:  first check for pileups, then bams
        # Note:
        #    - Use the get_sam_data() method when testing locally on a
        #      .sam file or using the pileups files
        #    - Use the get_bam_data() method when querying the entire
        #      chromosome, coordinate ranges from the coordinates file,
        #      or one coordinate range via the -a to -z params

        # Use the get_sam_data() method when testing locally on a .sam file
        # or using the pileups files
        if (i_dnaNormalPileupsFilename is not None):
            i_dnaNormalGenerator = get_sam_data(i_dnaNormalPileupsFilename,
                                                currentChrom,
                                                currentStart,
                                                currentStop,
                                                i_dnaNormLabel,
                                                i_debug)
        # Use the get_bam_data() method when querying the entire chromosome,
        # coordinate ranges from the coordinates file, or one coordinate range
        # via the -a to -z params
        elif (i_dnaNormalFilename is not None):
            # some bams/references use "M", some use "MT"
            if (i_chrom == "M" or
                i_chrom == "MT" and
                i_dnaNormMitochon is not None):
                i_dnaNormalGenerator = get_bam_data(i_dnaNormalFilename,
                                                    i_dnaNormalFastaFilename,
                                                    i_dnaNormMitochon,
                                                    currentStart,
                                                    currentStop,
                                                    i_batchSize,
                                                    i_dnaNormUseChr,
                                                    i_dnaNormLabel,
                                                    False,
                                                    i_debug)
            else:
                i_dnaNormalGenerator = get_bam_data(i_dnaNormalFilename,
                                                    i_dnaNormalFastaFilename,
                                                    currentChrom,
                                                    currentStart,
                                                    currentStop,
                                                    i_batchSize,
                                                    i_dnaNormUseChr,
                                                    i_dnaNormLabel,
                                                    False,
                                                    i_debug)

        # Use the get_sam_data() method when testing locally on a .sam file
        # or using the pileups files
        if (i_rnaNormalPileupsFilename is not None):
            i_rnaNormalGenerator = get_sam_data(i_rnaNormalPileupsFilename,
                                                currentChrom,
                                                currentStart,
                                                currentStop,
                                                i_rnaNormLabel,
                                                i_debug)
        # Use the get_bam_data() method when querying the entire chromosome,
        # coordinate ranges from the coordinates file, or one coordinate range
        # via the -a to -z params
        elif (i_rnaNormalFilename is not None):
            # some bams/reference use "M", some use "MT"
            if (i_chrom == "M" or
                i_chrom == "MT" and
                i_rnaNormMitochon is not None):
                i_rnaNormalGenerator = get_bam_data(i_rnaNormalFilename,
                                                    i_rnaNormalFastaFilename,
                                                    i_rnaNormMitochon,
                                                    currentStart,
                                                    currentStop,
                                                    i_batchSize,
                                                    i_rnaNormUseChr,
                                                    i_rnaNormLabel,
                                                    i_rnaIncludeSecAlignments,
                                                    i_debug)
            else:
                i_rnaNormalGenerator = get_bam_data(i_rnaNormalFilename,
                                                    i_rnaNormalFastaFilename,
                                                    currentChrom,
                                                    currentStart,
                                                    currentStop,
                                                    i_batchSize,
                                                    i_rnaNormUseChr,
                                                    i_rnaNormLabel,
                                                    i_rnaIncludeSecAlignments,
                                                    i_debug)

        # Use the get_sam_data() method when testing locally on a .sam file
        # or using the pileups files
        if (i_dnaTumorPileupsFilename is not None):
            i_dnaTumorGenerator = get_sam_data(i_dnaTumorPileupsFilename,
                                               currentChrom,
                                               currentStart,
                                               currentStop,
                                               i_dnaTumLabel,
                                               i_debug)
        # Use the get_bam_data() method when querying the entire chromosome,
        # coordinate ranges from the coordinates file, or one coordinate range
        # via the -a to -z params
        elif (i_dnaTumorFilename is not None):
            # some bams/reference use "M", some use "MT"
            if (i_chrom == "M" or
                i_chrom == "MT" and
                i_dnaTumMitochon is not None):
                i_dnaTumorGenerator = get_bam_data(i_dnaTumorFilename,
                                                   i_dnaTumorFastaFilename,
                                                   i_dnaTumMitochon,
                                                   currentStart,
                                                   currentStop,
                                                   i_batchSize,
                                                   i_dnaTumUseChr,
                                                   i_dnaTumLabel,
                                                   False,
                                                   i_debug)
            else:
                i_dnaTumorGenerator = get_bam_data(i_dnaTumorFilename,
                                                   i_dnaTumorFastaFilename,
                                                   currentChrom,
                                                   currentStart,
                                                   currentStop,
                                                   i_batchSize,
                                                   i_dnaTumUseChr,
                                                   i_dnaTumLabel,
                                                   False,
                                                   i_debug)

        # Use the get_sam_data() method when testing locally on a .sam file or
        # using the pileups files
        if (i_rnaTumorPileupsFilename is not None):
            i_rnaTumorGenerator = get_sam_data(i_rnaTumorPileupsFilename,
                                               currentChrom,
                                               currentStart,
                                               currentStop,
                                               i_rnaTumLabel,
                                               i_debug)
        # Use the get_bam_data() method when querying the entire chromosome,
        # coordinate ranges from the coordinates file, or one coordinate range
        # via the -a to -z params
        elif (i_rnaTumorFilename is not None):
            # some bams/reference use "M", some use "MT"
            if (i_chrom == "M" or
                i_chrom == "MT" and
                i_rnaTumMitochon is not None):
                i_rnaTumorGenerator = get_bam_data(i_rnaTumorFilename,
                                                   i_rnaTumorFastaFilename,
                                                   i_rnaTumMitochon,
                                                   currentStart,
                                                   currentStop,
                                                   i_batchSize,
                                                   i_rnaTumUseChr,
                                                   i_rnaTumLabel,
                                                   i_rnaIncludeSecAlignments,
                                                   i_debug)
            else:
                i_rnaTumorGenerator = get_bam_data(i_rnaTumorFilename,
                                                   i_rnaTumorFastaFilename,
                                                   currentChrom,
                                                   currentStart,
                                                   currentStop,
                                                   i_batchSize,
                                                   i_rnaTumUseChr,
                                                   i_rnaTumLabel,
                                                   i_rnaIncludeSecAlignments,
                                                   i_debug)

        # get the first pileup from each file
        # if a file is not specified, then the "moreLines" flags will be set
        # to false and initial values will be returned
        (moreDnaNormalLines,
         dnaNormalChr,
         dnaNormalCoordinate,
         dnaNormalRefBase,
         dnaNormalNumBases,
         dnaNormalReads,
         dnaNormalBaseQuals,
         dnaNormalMapQuals) = get_next_pileup(i_dnaNormalGenerator)
        (moreRnaNormalLines,
         rnaNormalChr,
         rnaNormalCoordinate,
         rnaNormalRefBase,
         rnaNormalNumBases,
         rnaNormalReads,
         rnaNormalBaseQuals,
         rnaNormalMapQuals) = get_next_pileup(i_rnaNormalGenerator)
        (moreDnaTumorLines,
         dnaTumorChr,
         dnaTumorCoordinate,
         dnaTumorRefBase,
         dnaTumorNumBases,
         dnaTumorReads,
         dnaTumorBaseQuals,
         dnaTumorMapQuals) = get_next_pileup(i_dnaTumorGenerator)
        (moreRnaTumorLines,
         rnaTumorChr,
         rnaTumorCoordinate,
         rnaTumorRefBase,
         rnaTumorNumBases,
         rnaTumorReads,
         rnaTumorBaseQuals,
         rnaTumorMapQuals) = get_next_pileup(i_rnaTumorGenerator)

        # for each coordinate that we'd like to investigate
        for currentCoordinate in xrange(currentStart, currentStop+1):

            # if the entire coordinate range has been loaded, but we're only
            # interested in the coordinates in the coordinates file, and this
            # coordinate is not in the file, then get the next coordinate
            if (i_loadCoordinatesRange and
                i_coordinatesFilename is not None and
                currentCoordinate not in i_lookupStarts):

                # if there are more lines, and the coordinate is <= the
                # current coordinate, then get the next pileup
                if (moreDnaNormalLines and
                    dnaNormalCoordinate <= currentCoordinate):
                    (moreDnaNormalLines,
                     dnaNormalChr,
                     dnaNormalCoordinate,
                     dnaNormalRefBase,
                     dnaNormalNumBases,
                     dnaNormalReads,
                     dnaNormalBaseQuals,
                     dnaNormalMapQuals) = get_next_pileup(i_dnaNormalGenerator)
                if (moreRnaNormalLines and
                    rnaNormalCoordinate <= currentCoordinate):
                    (moreRnaNormalLines,
                     rnaNormalChr,
                     rnaNormalCoordinate,
                     rnaNormalRefBase,
                     rnaNormalNumBases,
                     rnaNormalReads,
                     rnaNormalBaseQuals,
                     rnaNormalMapQuals) = get_next_pileup(i_rnaNormalGenerator)
                if (moreDnaTumorLines and
                    dnaTumorCoordinate <= currentCoordinate):
                    (moreDnaTumorLines,
                     dnaTumorChr,
                     dnaTumorCoordinate,
                     dnaTumorRefBase,
                     dnaTumorNumBases,
                     dnaTumorReads,
                     dnaTumorBaseQuals,
                     dnaTumorMapQuals) = get_next_pileup(i_dnaTumorGenerator)
                if (moreRnaTumorLines and
                    rnaTumorCoordinate <= currentCoordinate):
                    (moreRnaTumorLines,
                     rnaTumorChr,
                     rnaTumorCoordinate,
                     rnaTumorRefBase,
                     rnaTumorNumBases,
                     rnaTumorReads,
                     rnaTumorBaseQuals,
                     rnaTumorMapQuals) = get_next_pileup(i_rnaTumorGenerator)

                # skipping a lookup that we don't care about
                continue

            # for each coordinate
            #     if we have normal dna
            #         compare to reference -> germline mutations

            #     if we have normal rna-seq
            #        characterize germline variants
            #        identify normal rna-editing

            #     if we have tumor dna
            #         compare to reference and normal -> somatic mutations

            #     if we have tumor rna-seq
            #         characterize somatic mutations
            #         identify tumor rna-editing

            if (i_debug):
                logging.debug("currentCoordinate: %s", currentCoordinate)
                logging.debug("Initial NormalDNAData: %s %s %s %s %s %s %s",
                              dnaNormalChr, dnaNormalCoordinate,
                              dnaNormalRefBase, dnaNormalNumBases,
                              dnaNormalReads, dnaNormalBaseQuals,
                              dnaNormalMapQuals)
                logging.debug("Initial NormalRNAData: %s %s %s %s %s %s %s",
                              rnaNormalChr, rnaNormalCoordinate,
                              rnaNormalRefBase, rnaNormalNumBases,
                              rnaNormalReads, rnaNormalBaseQuals,
                              rnaNormalMapQuals)
                logging.debug("Initial TumorDNAData: %s %s %s %s %s %s %s",
                              dnaTumorChr, dnaTumorCoordinate, dnaTumorRefBase,
                              dnaTumorNumBases, dnaTumorReads,
                              dnaTumorBaseQuals, dnaTumorMapQuals)
                logging.debug("Initial TumorRNAData: %s %s %s %s %s %s %s",
                              rnaTumorChr, rnaTumorCoordinate, rnaTumorRefBase,
                              rnaTumorNumBases, rnaTumorReads,
                              rnaTumorBaseQuals, rnaTumorMapQuals)

            # if we are not looking up specific coordinates via the coordinates
            # file, and we don't have any more data, then break out of the loop
            # this can happen when testing, or when we've reached the end of
            # all the data in the .mpileups files
            if (i_coordinatesFilename is None and
                dnaNormalCoordinate == -1 and
                rnaNormalCoordinate == -1 and
                dnaTumorCoordinate == -1 and
                rnaTumorCoordinate == -1):
                break

            # empty the set of DNA for each new coordinate
            dnaSet.clear()
            altCountsDict.clear()
            infoDict.clear()
            del altList[:]
            del refList[:]
            del filterList[:]

            setMinTotalBasesFlag = True
            setMinAltBasesFlag = True
            shouldOutput = False
            hasDNA = False
            hasRNA = False
            totalSamples = 0
            totalReadDepth = 0
            totalInsCount = 0
            totalDelCount = 0
            totalStarts = 0
            totalStops = 0
            totalSumBaseQual = 0
            totalSumMapQual = 0
            totalSumMapQualZero = 0
            totalSumStrandBias = 0
            totalAltReadDepth = 0

            # create some default output in case there are no reads for
            # one dataset but there are for others
            # columnHeaders = ["CHROM", "POS", "ID", "REF", "ALT",
            #                  "QUAL", "FILTER", "INFO", "FORMAT"]
            vcfOutputList = [currentChrom, str(currentCoordinate), "."]
            formatItemCount = len(formatString.split(":"))
            # the default genotype should be '.' for haploid calls
            # (e.g. chrom Y) and './.' for diploid calls
            if (currentChrom != "Y"):
                emptyFormatList = ["./."] + ["."] * (formatItemCount - 1)
            else:
                emptyFormatList = ["."] * formatItemCount
            emptyFormatString = ":".join(emptyFormatList)
            dnaNormalOutputStr = emptyFormatString
            dnaTumorOutputStr = emptyFormatString
            rnaNormalOutputStr = emptyFormatString
            rnaTumorOutputStr = emptyFormatString

            # these are only used to determine the Germline parent of an LOH
            # the output shows which parent base has been lost in the tumor DNA
            # we need the 2 variables, b/c we want to skip over the normal RNA
            # and pass the normal DNA previous bases onto the tumor DNA to look
            # for an LOH.  in the future, we may want to output other "losses",
            # but for now, the previousUniqueBases is just a place-holder.
            previousUniqueBases = ""
            dnaNormalPreviousBases = ""

            # create the ref list for this coordinate
            if (dnaNormalCoordinate == currentCoordinate and
                dnaNormalRefBase not in refList):
                refList.append(dnaNormalRefBase)

            if (rnaNormalCoordinate == currentCoordinate and
                rnaNormalRefBase not in refList):
                refList.append(rnaNormalRefBase)

            if (dnaTumorCoordinate == currentCoordinate and
                dnaTumorRefBase not in refList):
                refList.append(dnaTumorRefBase)

            if (rnaTumorCoordinate == currentCoordinate and
                rnaTumorRefBase not in refList):
                refList.append(rnaTumorRefBase)

            # if we aren't debugging and we have an "N" in the ref or more
            # than one ref, then just ignore this coordinate and move on
            # to the next
            if (not i_debug and ("N" in refList or len(refList) > 1)):
                # if there are more lines, and the coordinate is <= the
                # current coordinate, then get the next pileup
                if (moreDnaNormalLines and
                    dnaNormalCoordinate <= currentCoordinate):
                    (moreDnaNormalLines,
                     dnaNormalChr,
                     dnaNormalCoordinate,
                     dnaNormalRefBase,
                     dnaNormalNumBases,
                     dnaNormalReads,
                     dnaNormalBaseQuals,
                     dnaNormalMapQuals) = get_next_pileup(i_dnaNormalGenerator)

                if (moreRnaNormalLines and
                    rnaNormalCoordinate <= currentCoordinate):
                    (moreRnaNormalLines,
                     rnaNormalChr,
                     rnaNormalCoordinate,
                     rnaNormalRefBase,
                     rnaNormalNumBases,
                     rnaNormalReads,
                     rnaNormalBaseQuals,
                     rnaNormalMapQuals) = get_next_pileup(i_rnaNormalGenerator)

                if (moreDnaTumorLines and
                    dnaTumorCoordinate <= currentCoordinate):
                    (moreDnaTumorLines,
                     dnaTumorChr,
                     dnaTumorCoordinate,
                     dnaTumorRefBase,
                     dnaTumorNumBases,
                     dnaTumorReads,
                     dnaTumorBaseQuals,
                     dnaTumorMapQuals) = get_next_pileup(i_dnaTumorGenerator)

                if (moreRnaTumorLines and
                    rnaTumorCoordinate <= currentCoordinate):
                    (moreRnaTumorLines,
                     rnaTumorChr,
                     rnaTumorCoordinate,
                     rnaTumorRefBase,
                     rnaTumorNumBases,
                     rnaTumorReads,
                     rnaTumorBaseQuals,
                     rnaTumorMapQuals) = get_next_pileup(i_rnaTumorGenerator)

                # continue to the next coordinate
                continue

            # if we have normal reads at the current position
            if (dnaNormalCoordinate == currentCoordinate):

                # specify the normal constants
                gainModType = "GERM"
                lossModType = "NOREF"

                # process the normal DNA
                (dnaNormalOutputStr,
                 dnaNormalPreviousBases,
                 dnaNormalPrevBaseCounts,
                 dnaNormalReadDPDict,
                 dnaNormalAltPercentDict,
                 dnaNormalCoordWithData,
                 dnaSet,
                 altList,
                 altCountsDict,
                 hasDNA,
                 shouldOutput,
                 numTotalBasesFilter,
                 numAltBasesFilter,
                 totalGerms,
                 totalNoRef,
                 infoDict,
                 numBases,
                 insCount,
                 delCount,
                 starts,
                 stops,
                 totalBaseQual,
                 totalMapQual,
                 totalMapQualZero,
                 totalStrandBias,
                 totalAltReadSupport) = find_variants(dnaNormalChr,
                                                      dnaNormalCoordinate,
                                                      dnaNormalRefBase,
                                                      dnaNormalNumBases,
                                                      dnaNormalReads,
                                                      dnaNormalBaseQuals,
                                                      dnaNormalMapQuals,
                                                      previousUniqueBases,
                                                      previousBaseCounts,
                                                      dnaNormalReadDPDict,
                                                      dnaNormalAltPercentDict,
                                                      dnaNormalCoordWithData,
                                                      dnaSet,
                                                      refList,
                                                      altList,
                                                      altCountsDict,
                                                      hasDNA,
                                                      shouldOutput,
                                                      totalGerms,
                                                      totalNoRef,
                                                      gainModType,
                                                      lossModType,
                                                      infoDict,
                                                      i_dnaNormMinTotalBases,
                                                      i_dnaNormMinAltBases,
                                                      i_dnaNormMinAltBases,
                                                      i_dnaNormMinBaseQual,
                                                      i_dnaNormMinMapQual,
                                                      "DNA_NORMAL",
                                                      i_genotypeMinDepth,
                                                      i_genotypeMinPct,
                                                      dnaNormalOutputStr,
                                                      i_debug)

                if (numBases > 0):
                    totalSamples += 1
                    totalReadDepth += numBases
                    totalInsCount += insCount
                    totalDelCount += delCount
                    totalStarts += starts
                    totalStops += stops
                    totalSumBaseQual += totalBaseQual
                    totalSumMapQual += totalMapQual
                    totalSumMapQualZero += totalMapQualZero
                    totalSumStrandBias += totalStrandBias
                    totalAltReadDepth += totalAltReadSupport
                    setMinTotalBasesFlag = (setMinTotalBasesFlag and
                                            numTotalBasesFilter)
                    setMinAltBasesFlag = (setMinAltBasesFlag and
                                          numAltBasesFilter)

            # if we have normal rna-seq reads at the current position
            if (rnaNormalCoordinate == currentCoordinate):

                # if either a normal or tumor file is specified, we will label
                # them as edits. if neither a normal file nor a tumor file is
                # specified, we will label them as variants
                if (i_dnaNormalFilename is None and
                    i_dnaTumorFilename is None and
                    i_dnaNormalPileupsFilename is None and
                    i_dnaTumorPileupsFilename is None):
                    gainModType = "RNA_NOR_VAR"
                else:
                    gainModType = "NOR_EDIT"
                lossModType = "NOTEXP"

                # this is temporary, b/c we don't want to output NOTEXP
                # right now. need to think about this in more detail
                previousUniqueBases = ""

                (rnaNormalOutputStr,
                 previousUniqueBases,
                 previousBaseCounts,
                 rnaNormalReadDPDict,
                 rnaNormalAltPercentDict,
                 rnaNormalCoordWithData,
                 dnaSet,
                 altList,
                 altCountsDict,
                 hasRNA,
                 shouldOutput,
                 numTotalBasesFilter,
                 numAltBasesFilter,
                 totalNormEdits,
                 totalNormNotExp,
                 infoDict,
                 numBases,
                 insCount,
                 delCount,
                 starts,
                 stops,
                 totalBaseQual,
                 totalMapQual,
                 totalMapQualZero,
                 totalStrandBias,
                 totalAltReadSupport) = find_variants(rnaNormalChr,
                                                      rnaNormalCoordinate,
                                                      rnaNormalRefBase,
                                                      rnaNormalNumBases,
                                                      rnaNormalReads,
                                                      rnaNormalBaseQuals,
                                                      rnaNormalMapQuals,
                                                      previousUniqueBases,
                                                      previousBaseCounts,
                                                      rnaNormalReadDPDict,
                                                      rnaNormalAltPercentDict,
                                                      rnaNormalCoordWithData,
                                                      dnaSet,
                                                      refList,
                                                      altList,
                                                      altCountsDict,
                                                      hasRNA,
                                                      shouldOutput,
                                                      totalNormEdits,
                                                      totalNormNotExp,
                                                      gainModType,
                                                      lossModType,
                                                      infoDict,
                                                      i_rnaNormMinTotalBases,
                                                      i_rnaNormMinAltBases,
                                                      i_dnaNormMinAltBases,
                                                      i_rnaNormMinBaseQual,
                                                      i_rnaNormMinMapQual,
                                                      "RNA_NORMAL",
                                                      i_genotypeMinDepth,
                                                      i_genotypeMinPct,
                                                      rnaNormalOutputStr,
                                                      i_debug)

                if (numBases > 0):
                    totalSamples += 1
                    totalReadDepth += numBases
                    totalInsCount += insCount
                    totalDelCount += delCount
                    totalStarts += starts
                    totalStops += stops
                    totalSumBaseQual += totalBaseQual
                    totalSumMapQual += totalMapQual
                    totalSumMapQualZero += totalMapQualZero
                    totalSumStrandBias += totalStrandBias
                    totalAltReadDepth += totalAltReadSupport
                    setMinTotalBasesFlag = (setMinTotalBasesFlag and
                                            numTotalBasesFilter)
                    setMinAltBasesFlag = (setMinAltBasesFlag and
                                          numAltBasesFilter)

            # if we have tumor reads at the current position
            if (dnaTumorCoordinate == currentCoordinate):

                # if a normal file is specified, we will label them as
                # somatic mutations, otherwise, we will just call them variants
                if (i_dnaNormalFilename is not None or
                    i_dnaNormalPileupsFilename is not None):
                    gainModType = "SOM"
                else:
                    gainModType = "DNA_TUM_VAR"
                lossModType = "LOH"

                # process the tumor DNA
                (dnaTumorOutputStr,
                 previousUniqueBases,
                 previousBaseCounts,
                 dnaTumorReadDPDict,
                 dnaTumorAltPercentDict,
                 dnaTumorCoordWithData,
                 dnaSet,
                 altList,
                 altCountsDict,
                 hasDNA,
                 shouldOutput,
                 numTotalBasesFilter,
                 numAltBasesFilter,
                 totalSoms,
                 totalLohs,
                 infoDict,
                 numBases,
                 insCount,
                 delCount,
                 starts,
                 stops,
                 totalBaseQual,
                 totalMapQual,
                 totalMapQualZero,
                 totalStrandBias,
                 totalAltReadSupport) = find_variants(dnaTumorChr,
                                                      dnaTumorCoordinate,
                                                      dnaTumorRefBase,
                                                      dnaTumorNumBases,
                                                      dnaTumorReads,
                                                      dnaTumorBaseQuals,
                                                      dnaTumorMapQuals,
                                                      dnaNormalPreviousBases,
                                                      dnaNormalPrevBaseCounts,
                                                      dnaTumorReadDPDict,
                                                      dnaTumorAltPercentDict,
                                                      dnaTumorCoordWithData,
                                                      dnaSet,
                                                      refList,
                                                      altList,
                                                      altCountsDict,
                                                      hasDNA,
                                                      shouldOutput,
                                                      totalSoms,
                                                      totalLohs,
                                                      gainModType,
                                                      lossModType,
                                                      infoDict,
                                                      i_dnaTumMinTotalBases,
                                                      i_dnaTumMinAltBases,
                                                      i_dnaNormMinAltBases,
                                                      i_dnaTumMinBaseQual,
                                                      i_dnaTumMinMapQual,
                                                      "DNA_TUMOR",
                                                      i_genotypeMinDepth,
                                                      i_genotypeMinPct,
                                                      dnaTumorOutputStr,
                                                      i_debug)

                if (numBases > 0):
                    totalSamples += 1
                    totalReadDepth += numBases
                    totalInsCount += insCount
                    totalDelCount += delCount
                    totalStarts += starts
                    totalStops += stops
                    totalSumBaseQual += totalBaseQual
                    totalSumMapQual += totalMapQual
                    totalSumMapQualZero += totalMapQualZero
                    totalSumStrandBias += totalStrandBias
                    totalAltReadDepth += totalAltReadSupport
                    setMinTotalBasesFlag = (setMinTotalBasesFlag and
                                            numTotalBasesFilter)
                    setMinAltBasesFlag = (setMinAltBasesFlag and
                                          numAltBasesFilter)

            # if we have tumor rna-seq reads at the current position
            if (rnaTumorCoordinate == currentCoordinate):

                # if either a normal or tumor file is specified, we will label
                # them as edits. if neither a normal file nor a tumor file is
                # specified, we will label them as variants
                if (i_dnaNormalFilename is None and
                    i_dnaTumorFilename is None and
                    i_dnaNormalPileupsFilename is None and
                    i_dnaTumorPileupsFilename is None):
                    gainModType = "RNA_TUM_VAR"
                else:
                    gainModType = "TUM_EDIT"
                lossModType = "NOTEXP"

                # this is temporary, b/c we don't want to output NOTEXP
                # right now. need to think about this in more detail
                previousUniqueBases = ""

                (rnaTumorOutputStr,
                 previousUniqueBases,
                 previousBaseCounts,
                 rnaTumorReadDPDict,
                 rnaTumorAltPercentDict,
                 rnaTumorCoordWithData,
                 dnaSet,
                 altList,
                 altCountsDict,
                 hasRNA,
                 shouldOutput,
                 numTotalBasesFilter,
                 numAltBasesFilter,
                 totalTumEdits,
                 totalTumNotExp,
                 infoDict,
                 numBases,
                 insCount,
                 delCount,
                 starts,
                 stops,
                 totalBaseQual,
                 totalMapQual,
                 totalMapQualZero,
                 totalStrandBias,
                 totalAltReadSupport) = find_variants(rnaTumorChr,
                                                      rnaTumorCoordinate,
                                                      rnaTumorRefBase,
                                                      rnaTumorNumBases,
                                                      rnaTumorReads,
                                                      rnaTumorBaseQuals,
                                                      rnaTumorMapQuals,
                                                      previousUniqueBases,
                                                      previousBaseCounts,
                                                      rnaTumorReadDPDict,
                                                      rnaTumorAltPercentDict,
                                                      rnaTumorCoordWithData,
                                                      dnaSet,
                                                      refList,
                                                      altList,
                                                      altCountsDict,
                                                      hasRNA,
                                                      shouldOutput,
                                                      totalTumEdits,
                                                      totalTumNotExp,
                                                      gainModType,
                                                      lossModType,
                                                      infoDict,
                                                      i_rnaTumMinTotalBases,
                                                      i_rnaTumMinAltBases,
                                                      i_dnaTumMinAltBases,
                                                      i_rnaTumMinBaseQual,
                                                      i_rnaTumMinMapQual,
                                                      "RNA_TUMOR",
                                                      i_genotypeMinDepth,
                                                      i_genotypeMinPct,
                                                      rnaTumorOutputStr,
                                                      i_debug)

                if (numBases > 0):
                    totalSamples += 1
                    totalReadDepth += numBases
                    totalInsCount += insCount
                    totalDelCount += delCount
                    totalStarts += starts
                    totalStops += stops
                    totalSumBaseQual += totalBaseQual
                    totalSumMapQual += totalMapQual
                    totalSumMapQualZero += totalMapQualZero
                    totalSumStrandBias += totalStrandBias
                    totalAltReadDepth += totalAltReadSupport
                    setMinTotalBasesFlag = (setMinTotalBasesFlag and
                                            numTotalBasesFilter)
                    setMinAltBasesFlag = (setMinAltBasesFlag and
                                          numAltBasesFilter)

            # count the number of ref mismatches
            if (len(refList) > 1):
                countRefMismatches += 1

            # hasDNA:  at least one DNA sample has enough total bases
            # hasRNA:  at least one RNA sample has enough total bases
            # shouldOutput:  at lease one sample has enough ALT bases

            # if we are outputting all data, or
            # if we should output, or
            # if we are debugging and we have data
            if (i_outputAllData or
                shouldOutput or
                (i_debug and hasDNA or hasRNA)):

                # the chrom, position, and Id columns have been filled
                # columnHeaders = ["CHROM", "POS", "ID", "REF", "ALT",
                #                  "QUAL", "FILTER", "INFO", "FORMAT"]

                # add the ref, alt, and score
                vcfOutputList.append(",".join(refList))
                # if we are outputting all data,
                # there might not be an ALT to
                # output, so default to '.'
                if (len(altList) == 0):
                    vcfOutputList.append(".")
                else:
                    vcfOutputList.append(",".join(altList))
                vcfOutputList.append("0")

                # add filters
                # if one of the references is "N", then set the filter
                if ("N" in refList):
                    filterList.append("noref")
                # if there is more than one reference, then set the filter
                if (len(refList) > 1):
                    filterList.append("diffref")
                # if none of the samples have enough total bases,
                # then set the filter
                if (setMinTotalBasesFlag):
                    filterList.append("mbt")
                # if none of the samples have enough ALT bases,
                # then set the filter
                if (setMinAltBasesFlag):
                    filterList.append("mba")
                # if there are no filters thus far, then pass it
                if (len(filterList) == 0):
                    filterList.append("PASS")

                # if we should output all data and we have data, or
                # if we should output and we pass the basic filters, or
                # if we are debugging, and this call doesn't pass
                if ((i_outputAllData and totalReadDepth > 0) or
                    (shouldOutput and "PASS" in filterList) or
                    (i_debug and "PASS" not in filterList)):
                    vcfOutputList.append(";".join(filterList))

                    # ##INFO=<ID=NS,Number=1,Type=Integer,
                    #    Description="Number of samples with data">
                    # ##INFO=<ID=AN,Number=1,Type=Integer,
                    #    Description="Total number of unique alleles
                    #                 across all samples">
                    # ##INFO=<ID=DP,Number=1,Type=Integer,
                    #    Description="Total read depth for all samples">
                    # ##INFO=<ID=DEL,Number=1,Type=Integer,
                    #    Description="Number of small deletions at this
                    #                 location in all samples">
                    # ##INFO=<ID=INS,Number=1,Type=Integer,
                    #    Description="Number of small insertions at this
                    #                 location in all samples">
                    # ##INFO=<ID=START,Number=1,Type=Integer,
                    #    Description="Number of reads that started at this
                    #                 location across all samples">
                    # ##INFO=<ID=STOP,Number=1,Type=Integer,
                    #    Description="Number of reads that stopped at this
                    #                 location across all samples">
                    # ##INFO=<ID=BQ,Number=1,Type=Integer,
                    #    Description="Overall average base quality">
                    # ##INFO=<ID=SB,Number=1,Type=Float,
                    #    Description="Overall average reads on plus strand">
                    # ##INFO=<ID=FA,Number=1,Type=Float,
                    #    Description="Overall fraction of reads
                    #                 supporting ALT">
                    # ##INFO=<ID=MT,Number=.,Type=String,
                    #    Description="Modification types at this location">
                    # ##INFO=<ID=MC,Number=.,Type=String,
                    #    Description="Modification base changes
                    #                 at this location">
                    # ##INFO=<ID=AC,Number=.,Type=Integer,
                    #    Description="Allele count in genotypes, for each ALT
                    #                 allele, in the same order as listed">
                    # ##INFO=<ID=AF,Number=.,Type=Float,
                    #    Description="Allele frequency, for each ALT allele,
                    #                 in the same order as listed">
                    # ##INFO=<ID=MQ,Number=1,Type=Integer,
                    #    Description="Overall average mapping quality">
                    # ##INFO=<ID=MQ0,Number=1,Type=Integer,
                    #    Description="Total Mapping Quality Zero Reads">
                    # ##INFO=<ID=MF,Number=.,Type=String,
                    #    Description="Modification filters applied to the
                    #                 filter types listed in MFT">
                    # ##INFO=<ID=MFT,Number=.,Type=String,
                    #    Description="Modification filter types at this
                    #                 position with format
                    #                 origin_modType_modChange">
                    # ##INFO=<ID=SOMATIC,Number=0,Type=Flag,
                    #    Description="Indicates if call is a somatic mutation">
                    # ##INFO=<ID=SS,Number=1,Type=Integer,
                    #    Description="Variant status relative to non-adjacent
                    #                 Normal,0=wildtype,1=germline,2=somatic,
                    #                 3=LOH,4=post-transcriptional
                    #                 modification,5=unknown">
                    # ##INFO=<ID=VT,Number=1,Type=String,
                    #    Description="Variant type, can be SNP, INS or DEL">

                    # add the alt counts and frequencies in the
                    # same order as the alt list
                    for base in altList:
                        altFreq = altCountsDict[base]/float(totalReadDepth)
                        infoDict["AC"].append(str(altCountsDict[base]))
                        infoDict["AF"].append(str(round(altFreq, 2)))

                    # add modTypes to info
                    infoDict["NS"].append(str(totalSamples))
                    infoDict["AN"].append(str(len(dnaSet)))
                    infoDict["DP"].append(str(totalReadDepth))
                    infoDict["INS"].append(str(totalInsCount))
                    infoDict["DEL"].append(str(totalDelCount))
                    infoDict["START"].append(str(totalStarts))
                    infoDict["STOP"].append(str(totalStops))
                    infoDict["MQ0"].append(str(totalSumMapQualZero))
                    infoDict["VT"].append("SNP")
                    if (totalReadDepth > 0):
                        avgBaseQual = totalSumBaseQual/float(totalReadDepth)
                        avgMapQual = totalSumMapQual/float(totalReadDepth)
                        avgSbias = totalSumStrandBias/float(totalReadDepth)
                        altFreq = totalAltReadDepth/float(totalReadDepth)
                        infoDict["BQ"].append(str(int(round(avgBaseQual, 0))))
                        infoDict["MQ"].append(str(int(round(avgMapQual, 0))))
                        infoDict["SB"].append(str(round(avgSbias, 2)))
                        infoDict["FA"].append(str(round(altFreq, 2)))

                    # add info
                    infoField = ""
                    for key in sorted(infoDict.iterkeys()):
                        if ("True" in infoDict[key]):
                            infoField += key + ";"
                        else:
                            infoField += key + "="
                            infoField += ",".join(infoDict[key]) + ";"

                    vcfOutputList.append(infoField.rstrip(";"))

                    # add format
                    vcfOutputList.append(formatString)

                    # add the sample specific data
                    alleleLen = len(refList + altList)
                    if (i_dnaNormalFilename is not None or
                        i_dnaNormalPileupsFilename is not None):
                        dnaNormalOutputStr = pad_output(dnaNormalOutputStr,
                                                        emptyFormatString,
                                                        alleleLen)
                        vcfOutputList.append(dnaNormalOutputStr)
                    if (i_rnaNormalFilename is not None or
                        i_rnaNormalPileupsFilename is not None):
                        rnaNormalOutputStr = pad_output(rnaNormalOutputStr,
                                                        emptyFormatString,
                                                        alleleLen)
                        vcfOutputList.append(rnaNormalOutputStr)
                    if (i_dnaTumorFilename is not None or
                        i_dnaTumorPileupsFilename is not None):
                        dnaTumorOutputStr = pad_output(dnaTumorOutputStr,
                                                       emptyFormatString,
                                                       alleleLen)
                        vcfOutputList.append(dnaTumorOutputStr)
                    if (i_rnaTumorFilename is not None or
                        i_rnaTumorPileupsFilename is not None):
                        rnaTumorOutputStr = pad_output(rnaTumorOutputStr,
                                                       emptyFormatString,
                                                       alleleLen)
                        vcfOutputList.append(rnaTumorOutputStr)

                    # if we should output
                    if (i_outputAllData or shouldOutput):
                        if (i_outputFileHandler is not None):
                            i_outputFileHandler.write("\t".join(vcfOutputList))
                            i_outputFileHandler.write("\n")
                        else:
                            print >> sys.stdout, "\t".join(vcfOutputList)
                    # if we're debugging
                    elif (i_debug):
                        logging.debug("\t".join(vcfOutputList))

            # count coordinates when we have both DNA and RNA
            if (hasDNA and hasRNA):
                countRnaDnaCoordinateOverlap += 1

            # if there are more lines, and the coordinate is <= the
            # current coordinate, then get the next pileup
            if (moreDnaNormalLines and
                dnaNormalCoordinate <= currentCoordinate):
                (moreDnaNormalLines,
                 dnaNormalChr,
                 dnaNormalCoordinate,
                 dnaNormalRefBase,
                 dnaNormalNumBases,
                 dnaNormalReads,
                 dnaNormalBaseQuals,
                 dnaNormalMapQuals) = get_next_pileup(i_dnaNormalGenerator)
            if (moreRnaNormalLines and
                rnaNormalCoordinate <= currentCoordinate):
                (moreRnaNormalLines,
                 rnaNormalChr,
                 rnaNormalCoordinate,
                 rnaNormalRefBase,
                 rnaNormalNumBases,
                 rnaNormalReads,
                 rnaNormalBaseQuals,
                 rnaNormalMapQuals) = get_next_pileup(i_rnaNormalGenerator)
            if (moreDnaTumorLines and
                dnaTumorCoordinate <= currentCoordinate):
                (moreDnaTumorLines,
                 dnaTumorChr,
                 dnaTumorCoordinate,
                 dnaTumorRefBase,
                 dnaTumorNumBases,
                 dnaTumorReads,
                 dnaTumorBaseQuals,
                 dnaTumorMapQuals) = get_next_pileup(i_dnaTumorGenerator)
            if (moreRnaTumorLines and
                rnaTumorCoordinate <= currentCoordinate):
                (moreRnaTumorLines,
                 rnaTumorChr,
                 rnaTumorCoordinate,
                 rnaTumorRefBase,
                 rnaTumorNumBases,
                 rnaTumorReads,
                 rnaTumorBaseQuals,
                 rnaTumorMapQuals) = get_next_pileup(i_rnaTumorGenerator)

    if (i_statsDir is not None):
        # output the variant counts
        i_varCountsFh = open(i_statsDir + "variantCounts.tab", "a")
        '''
        i_varCountsFh.write("\t".join([i_id,
                                       currentChrom,
                                       str(totalGerms),
                                       str(totalSoms),
                                       str(totalNormEdits),
                                       str(totalTumEdits),
                                       str(totalLohs)]) + "\n")
        '''
        i_varCountsFh.write("\t".join([i_id,
                                       currentChrom,
                                       str(totalGerms),
                                       str(totalSoms),
                                       str(totalNormEdits),
                                       str(totalTumEdits)]) + "\n")
        i_varCountsFh.close()

        # output the coordinates with data
        i_genStatsFh = open(i_statsDir + "genStats.tab", "a")
        i_genStatsFh.write("\t".join([i_id,
                                      currentChrom,
                                      str(dnaNormalCoordWithData),
                                      str(rnaNormalCoordWithData),
                                      str(dnaTumorCoordWithData),
                                      str(rnaTumorCoordWithData)]) + "\n")
        i_genStatsFh.close()

    stopTime = time.time()

    summaryMessage = "Summary for Chrom " + i_chrom + " and Id " + i_id + ": "
    if (i_dnaNormalFilename is not None or
        i_dnaNormalPileupsFilename is not None):
        # summaryMessage += "Total GERMs="
        # summaryMessage += str(totalGerms-totalLohs) + ", "
        summaryMessage += "Total GERMs="
        summaryMessage += str(totalGerms) + ", "
    if (i_rnaNormalFilename is not None or
        i_rnaNormalPileupsFilename is not None):
        summaryMessage += "Total Normal RNA Variants/Edits="
        summaryMessage += str(totalNormEdits) + ", "
    if (i_dnaTumorFilename is not None or
        i_dnaTumorPileupsFilename is not None):
        summaryMessage += "Total SOMs="
        summaryMessage += str(totalSoms) + ", "
        # summaryMessage += "Total LOHs="
        # summaryMessage += str(totalLohs) + ", "
    if (i_rnaTumorFilename is not None or
        i_rnaTumorPileupsFilename is not None):
        summaryMessage += "Total Tumor RNA Variants/Edits="
        summaryMessage += str(totalTumEdits) + ", "
        # summaryMessage += "Total coordinates with both DNA and RNA="
        # summaryMessage += str(countRnaDnaCoordinateOverlap) + ", "

    logging.info(summaryMessage.rstrip(", "))
    if (i_outputFilename is not None):
        logging.info("radia.py %s: Total time=%s hrs, %s mins, %s secs",
                     os.path.basename(i_outputFilename),
                     ((stopTime-startTime)/(3600)),
                     ((stopTime-startTime)/60),
                     (stopTime-startTime))
    else:
        logging.info("radia.py Chrom %s and Id %s: Total " +
                     "time=%s hrs, %s mins, %s secs",
                     currentChrom, i_id,
                     ((stopTime-startTime)/(3600)),
                     ((stopTime-startTime)/60),
                     (stopTime-startTime))

    # close the files
    if (i_outputFilename is not None):
        i_outputFileHandler.close()
    return


main()
sys.exit(0)

#!/usr/bin/env python3

import pandas as pd
import os
import glob
import shutil
import networkx as nx
import multiprocessing
import logging
from subprocess import call

# !!! This is just for testing purposes, obviously
import sys
sys.path.append('/home/mattolm/Programs/drep/')
import drep_modules as dm

"""
Bdb = pandas DataFrame with the columns genome and location
Mdb = pandas containing raw MASH information
Cdb = pandas containing clustering information (both MASH and ANIn)
"""

""" Program architecture:

*   The main method is cluster_genomes(). This method will take all necessary arguments
    for a normal clustering pipeline, and return all normal outputs

*   The method called by the main program is d_cluster_wrapper(). This method will take
    the raw arguments and workDirectory, parse and extract the needed info, and then call
    cluster_genomes(). It will then take the output information and save it to the
    WorkDirectory object.
"""

def cluster_genomes(Bdb,data_folder,MASH_ANI=.90,ANIn=.99,dry=False,
                    skipMash=False, skipANIn=False):
    """
    Takes a number of command line arguments and returns a couple pandas dataframes

    Required Input:
    * Bdb           - pandas dataframe with the columns "genome" and "location"
    * data_folder   - location where MASH and ANIn data will be stored
    
    Optional Input:
    
    ***** Clustering Arguments *****
    * MASH_ANI      - ANI threshold for clustering MASH
    * skipMash      - If true, skip MASH altogether
    
    * ANIn          - ANI threshold for clustering ANIn
    * ANIn_cov      - Coverage threshold for clustering ANIn
    * skipANIn      - If true, skip ANIn clustering altogether
    
    ***** Algorithm Arguments *****
    * MASH_kmers    - kmers to form during MASH comparisons
    
    ***** Other Arguments *****
    * dry           - don't actually do anything, just print commands
    * threads       - number of threads to use for analysis
    * overwrite     - overwrite existing data
    
    Returned Output:
    * Mdb = MASH comparison specifics
    * Ndb = ANIn comparison specifics
    * Cdb = Clustering information
    """
    
    logging.info(
    "Step 1. Parse Arguments")
    
    logging.info(
    "Step 2. Perform MASH clustering")
    if not skipMash:

        logging.info(
        "2a. Run pair-wise MASH clustering")
        Mdb = all_vs_all_MASH(Bdb, data_folder, dry=dry)
        
        logging.info(
        "2b. Cluster pair-wise MASH clustering")
        Cdb = cluster_database(Mdb,MASH_ANI)
        
    else:
        Cdb = gen_nomash_cdb(Bdb)
        
    logging.info(
    "Step 3. Perform ANIn clustering")
    if not skipANIn:
    
        logging.info(
        "3a. Run pair-wise ANIn within Cdb clusters")
        Ndb = run_anin_on_clusters(Bdb, Cdb, data_folder, dry=dry)
        
        logging.info(
        "3b. Cluster pair-wise ANIn within Cdb clusters")
        Cdb = cluster_anin_database(Cdb, Ndb, ANIn=.99, cov_thresh=0.5)

    logging.info(
    "Step 4. Return output")
    
    return Cdb,Mdb,Ndb

def d_cluster_wrapper(args,workDirectory):
    
    # Load the WorkDirectory.
    logging.info("Loading work directory")
    workDirectory = drep_modules.WorkDirectory.WorkDirectory(args.work_directory)
    logging.info(str(workDirectory))
    
    # Get all the arguments to call cluster_genomes().
    Bdb = parse_arguments(args,workDirectory)
    
def parse_arguments(args,workDirectory):
    
    # If genomes are provided, load them
    if args.genomes != None:
        assert workDirectory.hasDb("Bdb") == False, \
        "Must either provide a genome list, or run the 'filter' operation with the same work directory"
        Bdb = load_genomes(args.genomes)
        
    # If genomes are not provided, don't load them
    if args.genomes == None:
        assert workDirectory.hasDb("Bdb") != False, \
        "Must either provide a genome list, or run the 'filter' operation with the same work directory"
        
    return Bdb
    
def cluster_anin_database(Cdb, Ndb, ANIn=.99, cov_thresh=0.5):
    
    Table = {'genome':[],'ANIn_cluster':[]}

    # For every MASH cluster-
    for cluster in Cdb['MASH_cluster'].unique():
        # Filter the database to this cluster
        d = Ndb[Ndb['reference'].isin(Cdb['genome'][Cdb['MASH_cluster'] == cluster].tolist())]
        
        # Make a graph of the genomes in this cluster based on Ndb 
        g = make_graph_anin(d,cov_thresh=cov_thresh,anin_thresh=ANIn)
        df = cluster_graph(g)
    
        # For every ANIn cluster in this graph -
        for clust in df['cluster'].unique():
            # Filter the database to this cluster
            d = df[df['cluster'] == clust]
            
            # For every genome in this cluster-            
            for genome in d['genome'].tolist():
                                
                # Save the cluster information
                Table['genome'].append(genome)
                Table['ANIn_cluster'].append("{0}_{1}".format(cluster,clust))
    
    Gdb = pd.DataFrame(Table)
    
    return pd.merge(Gdb,Cdb)

def run_anin_on_clusters(Bdb, Cdb, data_folder, dry=False):
    """
    For each cluster in Cdb, run pairwise ANIn
    """
    
    # Set up folders
    ANIn_folder = data_folder + 'ANIn_files/'
    dm.make_dir(ANIn_folder,dry)
    
    # Add cluster information to Cdb
    Bdb = pd.merge(Bdb,Cdb)
    
    # Step 1. Make the directories and generate the list of commands to be run    
    
    cmds = []
    for cluster in Bdb['MASH_cluster'].unique():
        d = Bdb[Bdb['MASH_cluster'] == cluster]
        genomes = d['location'].tolist()
        outf = "{0}{1}/".format(ANIn_folder,cluster)
        dm.make_dir(outf,dry)
        cmds += gen_nucmer_commands(genomes,outf,maxgap=1,noextend=True)
        
    # Step 2. Run the nucmer commands  
    
    if not dry:
        thread_nucmer_cmds(cmds)
        
    # Step 3. Parse the nucmer output
    
    Ndb = pd.DataFrame()
    org_lengths = {y:dm.fasta_length(x) for x,y in zip(Bdb['location'].tolist(),Bdb['genome'].tolist())}
    for cluster in Bdb['MASH_cluster'].unique():
        outf = "{0}{1}/".format(ANIn_folder,cluster)
        data = process_deltadir(outf, org_lengths)
        data['MASH_cluster'] = cluster
        Ndb = pd.concat([Ndb,data],ignore_index=True)
        
    return Ndb
        
def gen_nucmer_commands(genomes,outf,c=65,maxgap=90,noextend=False,method='mum'):
    cmds = []
    for g1 in genomes:
        for g2 in genomes:
            out = "{0}{1}_vs_{2}".format(outf,get_genome_name_from_fasta(g1),get_genome_name_from_fasta(g2))
            cmds.append(gen_nucmer_cmd(out,g1,g2,c=c,noextend=noextend,maxgap=maxgap,method=method))
    
    return cmds

def run_nucmer_genomeList(genomes,outf,b2s,c=65,maxgap=90,noextend=False,method='mum',dry=False):
    """
    genomes is a list of locations of genomes in .fasta file. This will do pair-wise
    comparisons of those genomes using the nucmer settings given
    """
    # Run commands on biotite
    cmds = []
    for g1 in genomes:
        for g2 in genomes:
            out = "{0}{1}_vs_{2}".format(outf,get_genome_name_from_fasta(g1),get_genome_name_from_fasta(g2))
            cmds.append(gen_nucmer_cmd(out,g1,g2,c=c,noextend=noextend,maxgap=maxgap,method=method))
    if not dry:
        thread_nucmer_cmds(cmds)
        
    # Parse resulting folder
    data = process_deltadir(outf, b2s)
    data['c'] = c
    data['maxgap'] = maxgap
    data['noextend'] = noextend
    data['method'] = method
    
    return data
    
    
def all_vs_all_MASH(Bdb, data_folder, dry=False):
    """
    Run MASH pairwise within all samples in Bdb
    """
    
    # Set up folders
    MASH_folder = data_folder + 'MASH_files/'
    dm.make_dir(MASH_folder,dry)
    
    sketch_folder = MASH_folder + 'sketches/'
    if not dry:
        if os.path.exists(sketch_folder):
	        shutil.rmtree(sketch_folder)
        os.makedirs(sketch_folder)
    
    # Make the MASH sketches
    for fasta in Bdb['location'].unique():
        genome = Bdb['genome'][Bdb['location'] == fasta].tolist()[0]
        cmd = ['/opt/bin/bio/mash','sketch',fasta,'-o',sketch_folder + genome]
        cmd = ' '.join(cmd)
        dm.run_cmd(cmd,dry,True)
        
    # Combine MASH sketches
    cmd = ['/opt/bin/bio/mash','paste',MASH_folder + 'ALL.msh',sketch_folder + '*']
    cmd = ' '.join(cmd)
    dm.run_cmd(cmd,dry,True)
    
    # Calculate distances
    all_file = MASH_folder + 'ALL.msh'
    cmd = ['/opt/bin/bio/mash','dist',all_file,all_file,'>',MASH_folder + 'MASH_table.tsv']
    cmd = ' '.join(cmd)
    dm.run_cmd(cmd,dry,True)
    
    # Make Mdb
    Mdb = pd.DataFrame()
    file = MASH_folder + 'MASH_table.tsv'
    
    table = pd.read_csv(file,sep='\t',header = None)
    table.columns = ['genome1','genome2','dist','p','kmers']
    table['genome1'] = table['genome1'].apply(get_genome_name_from_fasta)
    table['genome2'] = table['genome2'].apply(get_genome_name_from_fasta)
    Mdb = pd.concat([Mdb,table],ignore_index=True)
    Mdb['similarity'] = 1 - Mdb['dist'].astype(float)
    
    return Mdb
    
def cluster_database(db, threshold):
    """
    Cluster a database which has the columns "genome1", "genome2", "similarity"
    
    Return the same database with the column "cluster" added
    """ 
    g = make_graph(db, threshold)
    Cdb = cluster_graph(g)
    Cdb = Cdb.rename(columns={'cluster':'MASH_cluster'})
    
    return Cdb
    

def make_graph_anin(df,cov_thresh=0.5,anin_thresh = 0.99):
    G = nx.Graph()
    for genome in df['reference'].unique().tolist():
        G.add_node(genome)
    for index, row in df.iterrows():
        if (row['ref_coverage'] > cov_thresh) & (row['ani'] > anin_thresh):
            G.add_edge(row['reference'],row['querry'])
    return G

def make_graph(df,threshold):
    G = nx.Graph()
    for genome in df['genome1'].unique().tolist():
        G.add_node(genome)
    for index, row in df.iterrows():
        sim = row['similarity']
        if sim > threshold:
            G.add_edge(row['genome1'],row['genome2'],sim=sim)
    return G

def cluster_graph(G):
    sub_graphs = nx.connected_component_subgraphs(G)
    table = {'cluster':[],'genome':[]}
    cluster = 0
    for g in sub_graphs:
        for genome in g.nodes():
            table['cluster'].append(cluster)
            table['genome'].append(genome)
        cluster += 1
    df = pd.DataFrame(table)
    return df
    
def get_genome_name_from_fasta(fasta):
    return str(os.path.basename(fasta).split('.')[0])
    
# Parse NUCmer delta file to get total alignment length and total sim_errors
def parse_delta(filename):
    """Returns (alignment length, similarity errors) tuple from passed .delta.
    - filename - path to the input .delta file
    Extracts the aligned length and number of similarity errors for each
    aligned uniquely-matched region, and returns the cumulative total for
    each as a tuple.
    """
    aln_length, sim_errors = 0, 0
    for line in [l.strip().split() for l in open(filename, 'rU').readlines()]:
        if line[0] == 'NUCMER' or line[0].startswith('>'):  # Skip headers
            continue
        # We only process lines with seven columns:
        if len(line) == 7:
            aln_length += abs(int(line[1]) - int(line[0]))
            sim_errors += int(line[4])
    return aln_length, sim_errors

def process_deltadir(delta_dir, org_lengths, logger=None):
    """Returns a tuple of ANIm results for .deltas in passed directory.
    - delta_dir - path to the directory containing .delta files
    - org_lengths - dictionary of total sequence lengths, keyed by sequence
    Returns the following pandas dataframes in a tuple; query sequences are
    rows, subject sequences are columns:
    - alignment_lengths - symmetrical: total length of alignment
    - percentage_identity - symmetrical: percentage identity of alignment
    - alignment_coverage - non-symmetrical: coverage of query and subject
    - similarity_errors - symmetrical: count of similarity errors
    May throw a ZeroDivisionError if one or more NUCmer runs failed, or a
    very distant sequence was included in the analysis.
    """
    # Process directory to identify input files
    deltafiles = glob.glob(delta_dir + '*.delta')

    Table = {'querry':[],'reference':[],'alignment_length':[],'similarity_errors':[],'ref_coverage':[],'querry_coverage':[],'ani':[]}
        
    # Process .delta files assuming that the filename format holds:
    # org1_vs_org2.delta
    zero_error = False  # flag to register a divide-by-zero error
    for deltafile in deltafiles:
        qname, sname = os.path.splitext(os.path.split(deltafile)[-1])[0].split('_vs_')
        tot_length, tot_sim_error = parse_delta(deltafile)
        if tot_length == 0 and logger is not None:
            print("Total alignment length reported in " +
                               "%s is zero!" % deltafile)
        query_cover = float(tot_length) / org_lengths[qname]
        sbjct_cover = float(tot_length) / org_lengths[sname]
        # Calculate percentage ID of aligned length. This may fail if
        # total length is zero.
        # The ZeroDivisionError that would arise should be handled
        # Common causes are that a NUCmer run failed, or that a very
        # distant sequence was included in the analysis.
        try:
            perc_id = 1 - float(tot_sim_error) / tot_length
        except ZeroDivisionError:
            #print("Alignment between {0} and {1} has 0 alignment!".format(qname,sname))
            perc_id = 0  # set arbitrary value of zero identity
            zero_error = True
        
        Table['querry'].append(qname)
        Table['reference'].append(sname)
        Table['alignment_length'].append(tot_length)
        Table['similarity_errors'].append(tot_sim_error)
        Table['ani'].append(perc_id)
        Table['ref_coverage'].append(sbjct_cover)
        Table['querry_coverage'].append(query_cover)
    
    df = pd.DataFrame(Table)
    return df
    
def gen_nucmer_cmd(prefix,ref,querry,c='65',noextend=False,maxgap='90',method='mum'):
    cmd = ['nucmer','--' + method,'-p',prefix,'-c',str(c),'-g',str(maxgap)]
    if noextend: cmd.append('--noextend')
    cmd += [ref,querry]
    return cmd

def thread_nucmer_cmds(cmds,t=10):
    pool = multiprocessing.Pool(processes=t)
    pool.map(run_nucmer_cmd,cmds)
    pool.close()
    pool.join()
    return
    
def run_nucmer_cmd(cmd,dry=False,shell=False):
    if shell:
        if not dry: call(cmd,shell=True)
        else: print(cmd)
    else: 
        if not dry: call(cmd)
        else: print(' '.join(cmd))
    return
    
def load_genomes(genome_list):
    Table = {'genome':[],'location':[]}

    for genome in genome_list: 
        assert os.path.isfile(genome)
        Table['genome'].append('.'.join(os.path.basename(genome).split('.')[:-1]))
        Table['location'].append(genome)
    
    Bdb = pd.DataFrame(Table)
    return Bdb
    
def test_clustering():

    # Get test genomes
	test_genomes = glob.glob(str(os.getcwd()) + '/../test/genomes/*')
	names = [os.path.basename(g) for g in test_genomes]
	assert names == ['Enterococcus_faecalis_T2.fna', 'Escherichia_coli_Sakai.fna', 'Enterococcus_casseliflavus_EC20.fasta', 'Enterococcus_faecalis_TX0104.fa', 'Enterococcus_faecalis_YI6-1.fna']
	Bdb = load_genomes(test_genomes)
	
	# Set test directory
	test_directory = str(os.getcwd()) + '/../test/test_backend/'
	if os.path.exists(test_directory):
	    shutil.rmtree(test_directory)
	os.makedirs(test_directory)
	
	# Perform functional test
	Cdb,Mdb,Ndb = cluster_genomes(Bdb,test_directory)
	
	# Confirm it's right by showing there is one group of 3 and two groups of one
	group_lengths = sorted([len(Cdb['genome'][Cdb['ANIn_cluster'] == x].tolist()) \
	            for x in Cdb['ANIn_cluster'].unique()])
	print(group_lengths)
	assert group_lengths == [1, 1, 3]
	
	print("Functional test success!")

if __name__ == '__main__':
	
	test_clustering()
	
	
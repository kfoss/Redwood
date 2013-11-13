#!/usr/bin/env python
#
# Copyright (c) 2013 In-Q-Tel, Inc/Lab41, All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
This Filter provides analysis and scoring based on feature clusters of individual
directories of files from media sources

Created on 19 October 2013
@author: Paul
"""


import time
import operator
import os
import numpy as np
import matplotlib.pyplot as plt
from collections import namedtuple, defaultdict
from hashlib import sha1
from scipy.cluster.vq import vq, kmeans, whiten
from redwood.filters.redwood_filter import RedwoodFilter
import calendar
import random
import warnings
from multiprocessing import Pool, Queue, Manager
import Queue

warnings.filterwarnings('ignore')

def find_anomalies(rows, sorted_results, sorted_code_counts):
    
    #definitely want to adjust these distance thresholds
    distance_threshold0 = 1.0
    distance_threshold1 = 1.25
    distance_threshold2 = 5.0

   #print "Code counts: {} smallest: {} ".format(sorted_code_counts, sorted_code_counts[0][0])
    smallest_count = sorted_code_counts[0][1]
    if smallest_count < 2:
        target = sorted_code_counts[0][0]  #smallest cluster
    else:
        target = -1


    for c, d, r in sorted_results:

        #a lone cluster with just 1 element
        if c == target:
            score = .3
        if d > distance_threshold2:
            score = .1
        elif d> distance_threshold1:
            score = .3
        elif d> distance_threshold0:
            score = .4
        else:
            score = 1
        file_metadata_id = r[0]
        rows.put((file_metadata_id, score))


def do_eval(rows, full_path, files, num_clusters, num_features):
    
    srt = time.time()
    num_obs = len(files)
   
    if(num_obs < num_clusters):
        return
 
    #zero out the array that will contain the inodes
    observations = np.zeros((num_obs, num_features))

    i = 0
    
    for inode,mod_date,_,_,_,_, in files:
        seconds = calendar.timegm(mod_date.utctimetuple())
        observations[i][0] = inode
        observations[i][1] = seconds
        i += 1
    
    whitened = whiten(observations) 

    #get the centroids
    codebook,_ = kmeans(whitened, num_clusters)
   
    #sometimes if all observations for a given feature are the same
    #the centroids will not be found.  For now, just throw out that data, but
    #figure out a solution later
    if len(codebook) != num_clusters:
        files = list()
        return

    code, dist = vq(whitened, codebook)
    d = defaultdict(int)

    #quick way to get count of cluster sizes        
    for c in code:
        d[c] += 1


    #sorted the map codes to get the smallest to largest cluster
    sorted_codes = sorted(d.iteritems(), key = operator.itemgetter(1))
    
    combined = zip(code, dist, files)
    sorted_results =  sorted(combined, key=lambda tup: tup[0])
    
    find_anomalies(rows, sorted_results, sorted_codes) 
     
    elp = time.time() - srt
    #print "completed pid {} time: {}".format(os.getpid(), elp)
             



class LocalityUniqueness(RedwoodFilter):

    """
    This LocalityUniqueness class provides the "Locality Uniqueness" filter functionality
    """

    def __init__(self, cnx=None):
        self.cnx = cnx
        self.name = "Locality Uniqueness"

    def usage(self):
        print "show_top [size]"
        print "\t--lists top size scores for all added sources"
        print "show_bottom [size]"
        print "\t--lists bottom size scores for all added sources"
        print "evaluate_dir [full_path] [source] [num_clusters]"
        print "\t--Runs kmeans and shows scatter plot"
    
    def update(self, source):
        """
        Applies the Locality Uniqueness filter to the given source, updating existing data
        analyzed from previous sources. Currently the update function uses 3 clusters for clustering
        analysis.  This will be dynamic in future versions. 

        :param source: media source name
        """
        
        self.build()
        self.evaluate_source(source, 3)
       

    def discover_show_top(self, n):
        """
        Discovery function that shows the top n scores from those media sources that already have been
        analyzed by this filter.

        :param n: the limit of results to show from the top results
        """

        cursor = self.cnx.cursor()
        query = ("""SELECT lu_scores.id, score,  unique_path.full_path, file_metadata.file_name, media_source.name from lu_scores 
                    LEFT JOIN file_metadata ON (lu_scores.id = file_metadata.unique_file_id) LEFT JOIN unique_path on 
                    (file_metadata.unique_path_id = unique_path.id) 
                    LEFT JOIN media_source on (file_metadata.source_id = media_source.id) order by score desc limit 0, {}""").format(n)
        cursor.execute(query)
        for (index, score, path, filename, source) in cursor:
            print "{} {} {} {} {}".format(index, score, path, filename, source)



    def discover_show_bottom(self, n):
        """
        Discovery function that shows the bottom n scores from those media sources that alreay have been
        analyzed by this filter

        :param n: the limit of results to show from the bottom results
        """

        cursor = self.cnx.cursor()
        query = ("""SELECT lu_scores.id, score,  unique_path.full_path, file_metadata.file_name, media_source.name from lu_scores 
                    LEFT JOIN file_metadata ON (lu_scores.id = file_metadata.unique_file_id) LEFT JOIN unique_path on 
                    (file_metadata.unique_path_id = unique_path.id) 
                    LEFT JOIN media_source on (file_metadata.source_id = media_source.id) order by score asc limit 0, {}""").format(n)
        cursor.execute(query)
        for(index, score, path, filename, source) in cursor:
            print "{}: [Score {}] [Src: {}] {}/{}".format(index, score, source,  path, filename)


    def discover_evaluate_dir(self, dir_name, source, num_clusters=3):
        """
        Discovery function that applies kmeans clustering to a specified directory. Currently,
        this function uses two static features of "modification date" and "inode number" but
        future versions will allow for dynamic features inputs.

        :param dir_name: directory name to be analyzed (Required)
        :source: source name to be analzyed (Required)
        :num_clusters: specified number of clusters to use for kmeans (Default: 3)
        """

        num_features = 2
        num_clusters = int(num_clusters)
        cursor = self.cnx.cursor()
        
        if(dir_name.endswith('/')):
            dir_name = dir_name[:-1]

        print "...Running discovery function on source {} at directory {}".format(source, dir_name)

        source_id = self.get_source_id(source)

        if source_id is -1:
            return [None, None]

        #grab all files for a particular directory from a specific source
        hash_val = sha1(dir_name).hexdigest()
        
        query = ("""SELECT file_name, file_metadata_id, filesystem_id, last_modified
        FROM joined_file_metadata
        WHERE source_id ='{}' AND path_hash = '{}' AND file_name !='/'""").format(source_id, hash_val)

        cursor.execute(query)

        #bring all results into memory
        sql_results = cursor.fetchall()
      
        if(len(sql_results) == 0):
            return [None, None]

        #zero out the array that will contain the inodes
        filesystem_id_arr = np.zeros((len(sql_results), num_features))

        i = 0
        for _, _,inode, mod_date in sql_results:
            seconds = calendar.timegm(mod_date.utctimetuple())
            filesystem_id_arr[i] = (inode, seconds)
            i += 1
        whitened = whiten(filesystem_id_arr) 
        #get the centroids
        codebook,_ = kmeans(whitened, num_clusters)
        code, dist = vq(whitened, codebook)
        d = defaultdict(int)

        #quick way to get count of cluster sizes        
        for c in code:
            d[c] += 1
      
        #sorted the map codes to get the smallest to largest cluster
        sorted_codes = sorted(d.iteritems(), key = operator.itemgetter(1))
        #sorts the codes and sql_results together as pairs
        combined = zip(dist, code, sql_results)
        sorted_results =  sorted(combined, key=lambda tup: tup[0])

        for dist_val, c, r in sorted_results:
            print "Dist: {} Cluster: {}  Data: {}".format(dist_val,c,r)

        self.visualize_scatter(d, code, whitened, codebook, 3, "inode number", "modification datetime")

    def evaluate_source(self, source_name, num_clusters=3):
        """
        Evaluates and scores a given source with a specified number of clusters for kmeans. Currently
        this function uses two set features as inputs (modification time and inode number), however
        futures versions will allow for dynamic feature inputs

        :param source_name: media source name
        :param num_clusters: number of clusters to input into kmeans (Default: 3)
        """

        cursor = self.cnx.cursor()
        
        cursor.execute(("""select id from media_source where name = '{}';""").format(source_name))
        
        r = cursor.fetchone()
        
        if r is None:
            print "Error: Source with name \"{}\" does not exist".format(source_name)
            return

        source_id = r[0]
        query = ("""SELECT count(*) from lu_analyzed_sources
                where id = '{}';""").format(source_id)

        cursor.execute(query)

        found = cursor.fetchone() 
        if found[0] > 0:
            print "already analyzed source {}".format(source_name)
            return
        else:
            query = "insert into lu_analyzed_sources (id, name) VALUES('{}','{}')".format(source_id, source_name)
            cursor.execute(query)
            self.cnx.commit()
            print "Evaluating {} ".format(source_name)
        #returns all files sorted by directory for the given source
        query = ("""SELECT file_metadata_id, last_modified, full_path, file_name, filesystem_id, parent_id, hash 
                FROM joined_file_metadata 
                where source_id = {} order by parent_id asc""").format(source_id)
        
        cursor.execute(query)
       
        files = list()

        print "...Beginning clustering analysis"
        pool = Pool(processes=4)              # start 4 worker processes
        manager = Manager()
        rows = manager.Queue()
        
        is_first = 0
        parent_id_prev = None
        #should iterate by dir of a given source at this point
        for(file_metadata_id, last_modified, full_path, file_name, filesystem_id, parent_id, hash_val) in cursor:
           
            if parent_id_prev != parent_id and is_first != 0:
                parent_id_prev = parent_id
                pool.apply_async(do_eval, [rows, full_path, files, num_clusters, 2])
                files = list()
            else:
                is_first = 1
                #make sure to omit directories
                
                if file_name != '/' and hash_val != "":
                    files.append((file_metadata_id, last_modified, full_path,file_name, filesystem_id, parent_id))
      
        pool.close()
        pool.join() 
           
        input_rows = []
        count = 0
        while rows.empty() is False:
            curr = rows.get()
            input_rows.append(curr)
            count +=1
            if count % 50000 is 0:
                print "...sending {} results to server".format(len(input_rows))
                cursor.executemany("""INSERT INTO locality_uniqueness(file_metadata_id, score) values(%s, %s)""", input_rows)
                input_rows = []
                count=0
        print "...sending {} results to server".format(len(input_rows))
        
        cursor.executemany("""INSERT INTO locality_uniqueness(file_metadata_id, score) values(%s, %s)""", input_rows)
        self.cnx.commit()
        #need to drop the lu_scores and recalculate
        cursor.execute("drop table if exists lu_scores")
        
        query = ("""CREATE TABLE IF NOT EXISTS `lu_scores` (
                `id` bigint(20) unsigned NOT NULL,
                `score` double DEFAULT NULL,
                KEY `fk_unique_file0_id` (`id`),
                CONSTRAINT `fk_unique_file0_id` FOREIGN KEY (`id`) 
                REFERENCES `unique_file` (`id`) ON DELETE NO ACTION ON UPDATE NO ACTION
                        ) ENGINE=InnoDB""")

        cursor.execute(query) 


        print "...updating scores on the server"
        query = ("""INSERT INTO lu_scores
                    (SELECT file_metadata.unique_file_id, avg(locality_uniqueness.score) FROM 
                    locality_uniqueness LEFT JOIN file_metadata on (locality_uniqueness.file_metadata_id = file_metadata.id) 
                    GROUP BY file_metadata.unique_file_id)""")            
       
        cursor.execute(query)
        self.cnx.commit()
        

         

    def clean(self):
        cursor = self.cnx.cursor()
        cursor.execute("DROP TABLE IF EXISTS lu_scores")
        cursor.execute("DROP TABLE IF EXISTS locality_uniqueness")
        cursor.execute("DROP TABLE IF EXISTS lu_analyzed_sources")
        self.cnx.commit()


    def build(self):

        cursor = self.cnx.cursor()

        query = ("CREATE TABLE IF NOT EXISTS lu_analyzed_sources ( "
                "id INT UNSIGNED NOT NULL,"
                "name VARCHAR(45) NOT NULL,"
                "PRIMARY KEY(id),"
                "CONSTRAINT fk_media_source_id FOREIGN KEY (id)"
                "REFERENCES media_source(id)"
                "ON DELETE NO ACTION ON UPDATE NO ACTION"
                ") ENGINE=InnoDB;")

        cursor.execute(query)
       
        self.cnx.commit()

        query = ("CREATE table IF NOT EXISTS locality_uniqueness ("
                "file_metadata_id BIGINT unsigned unique,"
                "score DOUBLE NOT NULL,"
                "PRIMARY KEY(file_metadata_id),"
                "INDEX lu_score (score ASC),"
                "CONSTRAINT fk_file_metadata FOREIGN KEY (file_metadata_id)"
                "REFERENCES file_metadata (id)"
                "ON DELETE NO ACTION ON UPDATE NO ACTION"
                 ") ENGINE = InnoDB;")
        
        cursor.execute(query)
        
        self.cnx.commit()
    


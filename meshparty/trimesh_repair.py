from annotationframeworkclient import chunkedgraph
from scipy import spatial, sparse
from meshparty import trimesh_io, utils
import pykdtree
import time
import numpy as np


def make_new_mesh_with_added_edges(mesh, new_edges):
    new_mesh = trimesh_io.MaskedMesh(mesh.vertices, mesh.faces, process=False)
    new_mesh._mesh_edges = np.concatenate([mesh.edges, new_edges])
    return new_mesh

def np_shared_rows(A,B):
    A_flat = np.ascontiguousarray(np.sort(A, axis=1)).view(dtype=('i8,i8'))
    B_flat = np.ascontiguousarray(np.sort(B, axis=1)).view(dtype=('i8,i8')) 
    good_edges = np.where(np.in1d(A_flat, B_flat))[0]
    return good_edges

def find_close_edges(vertices, labels, label_a, label_b):
    a_inds = np.where(labels==label_a)[0]
    b_inds = np.where(labels==label_b)[0]
    b_tree = spatial.cKDTree(vertices[b_inds,:], balanced_tree=False)
    ds, closest=b_tree.query(vertices[a_inds,:],k=1)
    b_close = b_inds[closest[~np.isinf(ds)]]
    a_close = a_inds[~np.isinf(ds)]
    return np.hstack((a_close[:,np.newaxis],b_close[:,np.newaxis]))

def find_close_edges_sym(vertices, labels, label_a, label_b):
    new_edges_a = find_close_edges(vertices,
                                   labels,
                                   label_a,
                                   label_b)
    new_edges_b = find_close_edges(vertices,
                                   labels,
                                   label_b,
                                   label_a)
    is_both = np_shared_rows(new_edges_a, new_edges_b)
    return new_edges_a[is_both,:]
    

def find_all_close_edges(vertices, labels, ccs, distance_upper_bound=2500):
    all_edges = []
    for i in range(ccs):
        for j in range(i,ccs):
            all_edges.append(find_close_edges_sym(vertices, labels, i, j, distance_upper_bound))
    return np.vstack(all_edges)

def find_edges_to_link(mesh, vert_ind_a, vert_ind_b):
    timings = {}
    start_time = time.time()
    
    # find the distance between the merge points and their center
    d = np.linalg.norm(mesh.vertices[vert_ind_a,:] - mesh.vertices[vert_ind_b,:])
    c = np.mean(mesh.vertices[[vert_ind_a, vert_ind_b],:], axis=0)
    # calculate the distance from the center to all other mesh points
    # ds = np.linalg.norm(mesh.vertices - c[np.newaxis,:], axis=1)
    inds = mesh.kdtree.query_ball_point(c, d*2)
    # make a mask that is twice the distance to the center
    #mask = ds<(d*2)
    mask = np.zeros(len(mesh.vertices), dtype=np.bool)
    mask[inds]=True
    
    timings['create_mask']=time.time()-start_time
    start_time = time.time()
    
    mask_mesh = mesh.apply_mask(mask)
    
    timings['apply_mask']=time.time()-start_time
    start_time = time.time()
    ccs, labels = sparse.csgraph.connected_components(mask_mesh.csgraph, return_labels=True)

    # map the original indices into this masked space
    mask_inds = mask_mesh.filter_unmasked_indices(np.array([vert_ind_a, vert_ind_b]))
    
    timings['masked_ccs']=time.time()-start_time
    start_time = time.time()
    
    # find all the multually closest edges between the linked components
    new_edges = find_close_edges_sym(mask_mesh.vertices,
                                 labels,
                                 labels[mask_inds[0]],
                                 labels[mask_inds[1]])
    timings['find_close_edges_sym']=time.time()-start_time
    start_time = time.time()

    
    if len(new_edges)==0:
        print('finding all close edges')
        new_edges =find_all_close_edges(mask_mesh.vertices, labels, ccs)
        print(f'new_edges shape {new_edges.shape}')
    if len(new_edges)==0:
        raise Exception('no close edges found')
    
    # create a new mesh that has these added edges
    new_mesh = make_new_mesh_with_added_edges(mask_mesh, new_edges)
    timings['make_new_mesh']=time.time()-start_time
    start_time = time.time()
    
    # find the shortest path to one of the linking spots in this new mesh
    d_ais_to_all, pred = sparse.csgraph.dijkstra(new_mesh.csgraph,
                                             indices=mask_inds[0],
                                             unweighted=False,
                                             directed=False,
                                             return_predecessors=True)   
    timings['find_close_edges_sym']=time.time()-start_time
    start_time = time.time()
    # make sure we found a good path
    if np.isinf(d_ais_to_all[mask_inds[1]]): 
        raise Exception(f"cannot find link between {vert_ind_a} and {vert_ind_b}")
   
    # turn this path back into a original mesh index edge list
    path = utils.get_path(mask_inds[0], mask_inds[1], pred)
    path_as_edges= utils.paths_to_edges([path])
    good_edges = np_shared_rows(path_as_edges, new_edges)
    good_edges = np.sort(path_as_edges[good_edges], axis=1)
    timings['remap answers']=time.time()-start_time
    print(timings)
    return mask_mesh.map_indices_to_unmasked(good_edges)
    #return np.sort(new_edges, axis=1)
    

def merge_edges_to_merge_indices(mesh, merge_edge_coords, close_map_distance = 300):

    Nmerge = merge_edge_coords.shape[0]
    # find the connected components of the mesh
    ccs, labels = sparse.csgraph.connected_components(mesh.csgraph, return_labels=True)
    uniq_labels, label_counts = np.unique(labels, return_counts=True)
    large_cc_mask = np.isin(labels, uniq_labels[label_counts>100])
    
    # convert the edge list to a Nmergex3 list of vertex positions
    merge_edge_verts = merge_edge_coords.reshape((merge_edge_coords.shape[0]*merge_edge_coords.shape[1],
                                              merge_edge_coords.shape[2]))
    # create a fake list of indices for a skeleton
    # merge_edge_inds = np.arange(0, len(merge_edge_verts)).reshape(Nmerge, 2)

    # make a kdtree of the mesh triangle centers
    tree = spatial.cKDTree(mesh.triangles_center, balanced_tree=False)
    # find the close triangle centers to the merge vertices
    ds2, close_face_inds = tree.query(merge_edge_verts)
    # pick one of the triangle faces as our close index
    close_inds = mesh.faces[close_face_inds,0]

    # lookup what connected component each close index is
    # reshaping it into a Nmerge x 2 format
    cc_labels = labels[close_inds].reshape((Nmerge,2))
   
    # we want to differeniate merges that connected different
    # connected components from those that are connected disconnected
    # components of the mesh
    is_join_merge = cc_labels[:,0]!=cc_labels[:,1]

    # however we can't trust that our mapping step worked perfectly
    # as euclidean distance can be tricky... so we do a second check
    # on the merge edges that end up on the same connected component
    # to see if there is another close vertex from a different connected
    # component

    # these are the merge edges that had 'close' results on at least one
    # of their edges
    close_mapped = np.min(ds2.reshape((Nmerge,2)), axis=1)<close_map_distance
    # calculate the index of merge edges that are mapped closely,
    # but ended up being on the same connected component
    # these are remapping candidates
    remap_merges = np.where(close_mapped & ~is_join_merge)[0]

    # pick out the coordinates of our candidates
    merge_coords = merge_edge_coords[close_mapped,:,:]
    # figure out which of the endpoint of the merge edge was the closest one
    close_merge = np.argmin(ds2.reshape((Nmerge,2)), axis=1)

    # we want to reconsider the other end of the merge edge and get their xyz coords
    far_points = merge_edge_coords[remap_merges,(1-close_merge)[remap_merges],:]
    # this is the connected component label for the close_merge end of the merge point
    closest_label = cc_labels[np.arange(0,len(cc_labels)), close_merge]
    # use a query_ball_point to find all the potential 'close' partners to the 
    # far point
    close_points = tree.query_ball_point(far_points, close_map_distance)

    # iterate over the far points
    for k, (close_cc, arr) in enumerate(zip(closest_label[remap_merges], close_points)):
        # k is the index of the remap candidate
        # close_cc is the connected component of the other side of that edge
        # arr is the list of candidate triangle center points

        # calculate a binary vector over the candidate points as to whether it is part 
        # of a different connected component than the other side of the merge edge
        other_comp_points = labels[mesh.faces[arr,0]]!= close_cc
        # find out how far each of the candidates that is on a different connected component is
        d = np.linalg.norm(far_points[k] - mesh.triangles_center[arr][other_comp_points], axis=1)

        # if this is empty that there are no candidates and we can safely ignore this merge edge
        if (d.shape[0]!=0):
            # otherwise there is another connected component that would be a 'close' match
            # this reindexs k to the 'far' point on the edge into the linear index of 'close_inds'
            ind = remap_merges[k]*2 + (1-close_merge[remap_merges[k]])
            # remap the index of the closest point with a different connected component
            # into the mesh vertex index.
            close_inds[ind] = mesh.faces[arr[np.where(other_comp_points)[0][np.argmin(d)]],0]
            # reset the is_join_merge index to True for this case
            is_join_merge[ind] = True

    # return the close_inds reshaped into Nmerge x 2, and filtered to only be those
    # that are connected differente connected components    
    return close_inds.reshape((Nmerge,2))[is_join_merge,:]

def get_merge_edges(mesh, seg_id, dataset_name, close_map_distance = 300):
    # initialize a chunkedgraph client
    cg_client = chunkedgraph.ChunkedGraphClient(dataset_name=dataset_name)
    
    # get the merge log 
    merge_log = cg_client.get_merge_log(seg_id)
    # convert the coordinates to numpy array and count them
    merge_edge_coords = np.array(merge_log['merge_edge_coords'])
    
    merge_edge_inds = merge_edges_to_merge_indices(mesh,
                                                   merge_edge_coords,
                                                   close_map_distance = close_map_distance)

    # iterate over these merge indices and find the link that 
    total_new_edges=[]
    for merge_ind in merge_edge_inds:
        new_edges = find_edges_to_link(mesh, merge_ind[0], merge_ind[1])
    total_new_edges.append(new_edges)
    
    # reshape the 
    new_edges = np.concatenate(total_new_edges)
    new_edges = new_edges.reshape((len(new_edges),2))

    return new_edges
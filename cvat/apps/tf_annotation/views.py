
# Copyright (C) 2018 Intel Corporation
#
# SPDX-License-Identifier: MIT

from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest, QueryDict
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render
from django.contrib.auth.decorators import permission_required
from cvat.apps.authentication.decorators import login_required
from cvat.apps.engine.models import Task as TaskModel
from cvat.apps.engine import annotation, task

import django_rq
import subprocess
import fnmatch
import logging
import json
import os
import rq

import tensorflow as tf
import numpy as np

from PIL import Image
from cvat.apps.engine.log import slogger

if os.environ.get('OPENVINO_TOOLKIT') == 'yes':
    from openvino.inference_engine import IENetwork, IEPlugin

def load_image_into_numpy(image):
    (im_width, im_height) = image.size
    return np.array(image.getdata()).reshape((im_height, im_width, 3)).astype(np.uint8)


def run_inference_engine_annotation(image_list, labels_mapping, treshold):
    def _check_instruction(instruction):
        return instruction == str.strip(
            subprocess.check_output(
                'lscpu | grep -o "{}" | head -1'.format(instruction), shell=True
            ).decode('utf-8')
        )

    def _normalize_box(box, w, h, dw, dh):
        xmin = min(int(box[0] * dw * w), w)
        ymin = min(int(box[1] * dh * h), h)
        xmax = min(int(box[2] * dw * w), w)
        ymax = min(int(box[3] * dh * h), h)
        return xmin, ymin, xmax, ymax

    result = {}
    MODEL_PATH = os.environ.get('TF_ANNOTATION_MODEL_PATH')
    if MODEL_PATH is None:
        raise OSError('Model path env not found in the system.')

    IE_PLUGINS_PATH = os.getenv('IE_PLUGINS_PATH')
    if IE_PLUGINS_PATH is None:
        raise OSError('Inference engine plugin path env not found in the system.')

    plugin = IEPlugin(device='CPU', plugin_dirs=[IE_PLUGINS_PATH])
    if (_check_instruction('avx2')):
        plugin.add_cpu_extension(os.path.join(IE_PLUGINS_PATH, 'libcpu_extension_avx2.so'))
    elif (_check_instruction('sse4')):
        plugin.add_cpu_extension(os.path.join(IE_PLUGINS_PATH, 'libcpu_extension_sse4.so'))
    else:
        raise Exception('Inference engine requires a support of avx2 or sse4.')

    network = IENetwork.from_ir(model = MODEL_PATH + '.xml', weights = MODEL_PATH + '.bin')
    input_blob_name = next(iter(network.inputs))
    output_blob_name = next(iter(network.outputs))
    executable_network = plugin.load(network=network)
    job = rq.get_current_job()

    del network

    try:
        for image_num, im_name in enumerate(image_list):

            job.refresh()
            if 'cancel' in job.meta:
                del job.meta['cancel']
                job.save()
                return None
            job.meta['progress'] = image_num * 100 / len(image_list)
            job.save_meta()

            image = Image.open(im_name)
            width, height = image.size
            image.thumbnail((600, 600), Image.ANTIALIAS)
            dwidth, dheight = 600 / image.size[0], 600 / image.size[1]
            image = image.crop((0, 0, 600, 600))
            image_np = load_image_into_numpy(image)
            image_np = np.transpose(image_np, (2, 0, 1))
            prediction = executable_network.infer(inputs={input_blob_name: image_np[np.newaxis, ...]})[output_blob_name][0][0]
            for obj in prediction:
                obj_class = int(obj[1])
                obj_value = obj[2]
                if obj_class and obj_class in labels_mapping and obj_value >= treshold:
                    label = labels_mapping[obj_class]
                    if label not in result:
                        result[label] = []
                    xmin, ymin, xmax, ymax = _normalize_box(obj[3:7], width, height, dwidth, dheight)
                    result[label].append([image_num, xmin, ymin, xmax, ymax])
    finally:
        del executable_network
        del plugin

    return result


def run_tensorflow_annotation(image_list, labels_mapping, treshold):
    def _normalize_box(box, w, h):
        xmin = int(box[1] * w)
        ymin = int(box[0] * h)
        xmax = int(box[3] * w)
        ymax = int(box[2] * h)
        return xmin, ymin, xmax, ymax

    result = {}
    model_path = os.environ.get('TF_ANNOTATION_MODEL_PATH')
    if model_path is None:
        raise OSError('Model path env not found in the system.')
    job = rq.get_current_job()

    detection_graph = tf.Graph()
    with detection_graph.as_default():
        od_graph_def = tf.GraphDef()
        with tf.gfile.GFile(model_path + '.pb', 'rb') as fid:
            serialized_graph = fid.read()
            od_graph_def.ParseFromString(serialized_graph)
            tf.import_graph_def(od_graph_def, name='')

        try:
            config = tf.ConfigProto()
            config.gpu_options.allow_growth=True
            sess = tf.Session(graph=detection_graph, config=config)
            for image_num, image_path in enumerate(image_list):

                job.refresh()
                if 'cancel' in job.meta:
                    del job.meta['cancel']
                    job.save()
                    return None
                job.meta['progress'] = image_num * 100 / len(image_list)
                job.save_meta()

                image = Image.open(image_path)
                width, height = image.size
                if width > 1920 or height > 1080:
                    image = image.resize((width // 2, height // 2), Image.ANTIALIAS)
                image_np = load_image_into_numpy(image)
                image_np_expanded = np.expand_dims(image_np, axis=0)

                image_tensor = detection_graph.get_tensor_by_name('image_tensor:0')
                boxes = detection_graph.get_tensor_by_name('detection_boxes:0')
                scores = detection_graph.get_tensor_by_name('detection_scores:0')
                classes = detection_graph.get_tensor_by_name('detection_classes:0')
                num_detections = detection_graph.get_tensor_by_name('num_detections:0')
                (boxes, scores, classes, num_detections) = sess.run([boxes, scores, classes, num_detections], feed_dict={image_tensor: image_np_expanded})

                for i in range(len(classes[0])):
                    if classes[0][i] in labels_mapping.keys():
                        if scores[0][i] >= treshold:
                            xmin, ymin, xmax, ymax = _normalize_box(boxes[0][i], width, height)
                            label = labels_mapping[classes[0][i]]
                            if label not in result:
                                result[label] = []
                            result[label].append([image_num, xmin, ymin, xmax, ymax])
        finally:
            sess.close()
            del sess
    return result


def make_image_list(path_to_data):
    def get_image_key(item):
        return int(os.path.splitext(os.path.basename(item))[0])

    image_list = []
    for root, dirnames, filenames in os.walk(path_to_data):
        for filename in fnmatch.filter(filenames, '*.jpg'):
                image_list.append(os.path.join(root, filename))

    image_list.sort(key=get_image_key)
    return image_list


def convert_to_cvat_format(data):
    def create_anno_container():
        return {
            "boxes": [],
            "polygons": [],
            "polylines": [],
            "points": [],
            "box_paths": [],
            "polygon_paths": [],
            "polyline_paths": [],
            "points_paths": [],
        }

    result = {
        'create': create_anno_container(),
        'update': create_anno_container(),
        'delete': create_anno_container(),
    }

    client_idx = 0
    for label in data:
        boxes = data[label]
        for box in boxes:
            result['create']['boxes'].append({
                "label_id": label,
                "frame": box[0],
                "xtl": box[1],
                "ytl": box[2],
                "xbr": box[3],
                "ybr": box[4],
                "z_order": 0,
                "group_id": 0,
                "occluded": False,
                "attributes": [],
                "id": client_idx,
            })

            client_idx += 1

    return result

def create_thread(tid, labels_mapping):
    try:
        TRESHOLD = 0.5
        # Init rq job
        job = rq.get_current_job()
        job.meta['progress'] = 0
        job.save_meta()
        # Get job indexes and segment length
        db_task = TaskModel.objects.get(pk=tid)
        db_segments = list(db_task.segment_set.prefetch_related('job_set').all())
        segment_length = max(db_segments[0].stop_frame - db_segments[0].start_frame + 1, 1)
        job_indexes = [segment.job_set.first().id for segment in db_segments]
        # Get image list
        image_list = make_image_list(db_task.get_data_dirname())

        # Run auto annotation by tf
        result = None
        if os.environ.get('CUDA_SUPPORT') == 'yes' or os.environ.get('OPENVINO_TOOLKIT') != 'yes':
            slogger.glob.info("tf annotation with tensorflow framework for task {}".format(tid))
            result = run_tensorflow_annotation(image_list, labels_mapping, TRESHOLD)
        else:
            slogger.glob.info('tf annotation with openvino toolkit for task {}'.format(tid))
            result = run_inference_engine_annotation(image_list, labels_mapping, TRESHOLD)

        if result is None:
            slogger.glob.info('tf annotation for task {} canceled by user'.format(tid))
            return

        # Modify data format and save
        result = convert_to_cvat_format(result)
        annotation.save_task(tid, result)
        slogger.glob.info('tf annotation for task {} done'.format(tid))
    except:
        try:
            slogger.task[tid].exception('exception was occured during tf annotation of the task', exc_info=True)
        except:
            slogger.glob.exception('exception was occured during tf annotation of the task {}'.format(tid), exc_into=True)

@login_required
def get_meta_info(request):
    try:
        queue = django_rq.get_queue('low')
        tids = json.loads(request.body.decode('utf-8'))
        result = {}
        for tid in tids:
            job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
            if job is not None:
                result[tid] = {
                    "active": job.is_queued or job.is_started,
                    "success": not job.is_failed
                }

        return JsonResponse(result)
    except Exception as ex:
        slogger.glob.exception('exception was occured during tf meta request', exc_into=True)
        return HttpResponseBadRequest(str(ex))


@login_required
@permission_required(perm=['engine.view_task', 'engine.change_annotation'], raise_exception=True)
def create(request, tid):
    slogger.glob.info('tf annotation create request for task {}'.format(tid))
    try:
        db_task = TaskModel.objects.get(pk=tid)
        if not task.is_task_owner(request.user, tid):
            raise Exception('Not enought of permissions for tf annotation')

        queue = django_rq.get_queue('low')
        job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
        if job is not None and (job.is_started or job.is_queued):
            raise Exception("The process is already running")

        db_labels = db_task.label_set.prefetch_related('attributespec_set').all()
        db_labels = {db_label.id:db_label.name for db_label in db_labels}

        tf_annotation_labels = {
            "person": 1, "bicycle": 2, "car": 3, "motorcycle": 4, "airplane": 5,
            "bus": 6, "train": 7, "truck": 8, "boat": 9, "traffic_light": 10,
            "fire_hydrant": 11, "stop_sign": 13, "parking_meter": 14, "bench": 15,
            "bird": 16, "cat": 17, "dog": 18, "horse": 19, "sheep": 20, "cow": 21,
            "elephant": 22, "bear": 23, "zebra": 24, "giraffe": 25, "backpack": 27,
            "umbrella": 28, "handbag": 31, "tie": 32, "suitcase": 33, "frisbee": 34,
            "skis": 35, "snowboard": 36, "sports_ball": 37, "kite": 38, "baseball_bat": 39,
            "baseball_glove": 40, "skateboard": 41, "surfboard": 42, "tennis_racket": 43,
            "bottle": 44, "wine_glass": 46, "cup": 47, "fork": 48, "knife": 49, "spoon": 50,
            "bowl": 51, "banana": 52, "apple": 53, "sandwich": 54, "orange": 55, "broccoli": 56,
            "carrot": 57, "hot_dog": 58, "pizza": 59, "donut": 60, "cake": 61, "chair": 62,
            "couch": 63, "potted_plant": 64, "bed": 65, "dining_table": 67, "toilet": 70,
            "tv": 72, "laptop": 73, "mouse": 74, "remote": 75, "keyboard": 76, "cell_phone": 77,
            "microwave": 78, "oven": 79, "toaster": 80, "sink": 81, "refrigerator": 83,
            "book": 84, "clock": 85, "vase": 86, "scissors": 87, "teddy_bear": 88, "hair_drier": 89,
            "toothbrush": 90
            }

        labels_mapping = {}
        for key, labels in db_labels.items():
            if labels in tf_annotation_labels.keys():
                labels_mapping[tf_annotation_labels[labels]] = key

        if not len(labels_mapping.values()):
            raise Exception('No labels found for tf annotation')

        # Run tf annotation job
        queue.enqueue_call(func=create_thread,
            args=(tid, labels_mapping),
            job_id='tf_annotation.create/{}'.format(tid),
            timeout=604800)     # 7 days

        slogger.task[tid].info('tensorflow annotation job enqueued with labels {}'.format(labels_mapping))

    except Exception as ex:
        try:
            slogger.task[tid].exception("exception was occured during tensorflow annotation request", exc_info=True)
        except:
            pass
        return HttpResponseBadRequest(str(ex))

    return HttpResponse()

@login_required
@permission_required(perm='engine.view_task', raise_exception=True)
def check(request, tid):
    try:
        queue = django_rq.get_queue('low')
        job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
        if job is not None and 'cancel' in job.meta:
            return JsonResponse({'status': 'finished'})
        data = {}
        if job is None:
            data['status'] = 'unknown'
        elif job.is_queued:
            data['status'] = 'queued'
        elif job.is_started:
            data['status'] = 'started'
            data['progress'] = job.meta['progress']
        elif job.is_finished:
            data['status'] = 'finished'
            job.delete()
        else:
            data['status'] = 'failed'
            job.delete()

    except Exception:
        data['status'] = 'unknown'

    return JsonResponse(data)


@login_required
@permission_required(perm='engine.view_task', raise_exception=True)
def cancel(request, tid):
    try:
        queue = django_rq.get_queue('low')
        job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
        if job is None or job.is_finished or job.is_failed:
            raise Exception('Task is not being annotated currently')
        elif 'cancel' not in job.meta:
            job.meta['cancel'] = True
            job.save()

    except Exception as ex:
        try:
            slogger.task[tid].exception("cannot cancel tensorflow annotation for task #{}".format(tid), exc_info=True)
        except:
            pass
        return HttpResponseBadRequest(str(ex))

    return HttpResponse()

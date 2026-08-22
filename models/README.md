# Blink detection models

Cullumi packages these models for offline CPU inference. Runtime model integrity
is checked before the first ONNX session is created.

| File | Upstream | SHA-256 |
| --- | --- | --- |
| `face_detection_yunet_2023mar.onnx` | [OpenCV Zoo YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `ocec_c.onnx` | [PINTO0309/OCEC, `onnx` release](https://github.com/PINTO0309/OCEC/releases/tag/onnx) | `779f6395bab036667f7652dce4e42cf84cb322a4f47600485fe07dddc6905749` |

YuNet and OCEC are distributed under the MIT license. Their license texts are
included beside the models. ONNX Runtime is also MIT licensed; its license is
included because the portable build bundles its runtime libraries.

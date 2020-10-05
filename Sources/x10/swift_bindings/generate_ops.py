# Lint as: python3
from absl import app
from absl import flags
import re

import yaml

FLAGS = flags.FLAGS

flags.DEFINE_string("def_file", None, "path to list of ops")
flags.DEFINE_string("cc_output", None, "path for the generated cc file")
flags.DEFINE_string("swift_output", None, "path for the generated swift file")

HEADER = """// Autogenerated by codegen.py. Do not modify.
"""

builtin_types = {
    "TFPadding": ("enum TFPadding", lambda name: f"ToTFPadding({name})",
                  "tensorflow::Padding"),
    "TFDataFormat":
        ("enum TFDataFormat", lambda name: f"x10::ToTFFormat({name})",
         "tensorflow::TensorFormat"),
    "TFMirrorPadMode":
        ("enum TFMirrorPadMode", lambda name: f"ToTFMirrorPadMode({name})",
         "tensorflow::MirrorPadMode"),
    "PaddingConfig":
        ("PaddingConfig", lambda name: f"ToXLAPaddingConfig({name})",
         "xla::PaddingConfig"),
}



def node_type_define(op):
  tensor_args = []
  attr_args = []
  has_tensor_list_arg = False
  for arg in op["args"]:
    if arg[1] == "Tensor":
      if has_tensor_list_arg:
        raise ValueError("[Tensor] must be the last argument")
      tensor_args.append(arg)
    elif arg[1] == "[Tensor]":
      if has_tensor_list_arg:
        raise ValueError("[Tensor] must be the last argument")
      tensor_args.append(arg)
      has_tensor_list_arg = True
    else: attr_args.append(arg)
  def format_pretty_print(arg):
    if arg[0] == "shape":
      return ""
    return f"    OpFieldToString(ss, \"{arg[0]}\", {arg[0]}_);\n"
  def format_ctor_arg(arg):
    name, stype, _ = arg
    if name == "shape":
      return f"xla::Shape {name}"
    if stype == "Tensor": return f"const Value& {name}"
    if stype == "[Tensor]":
      return f"absl::Span<const Value> {name}"
    if stype == "Int64": return f"xla::int64 {name}"
    if stype == "Bool": return f"bool {name}"
    if stype == "Float": return f"float {name}"
    if stype == "[Int64]":
      return f"std::vector<xla::int64> {name}"
    if stype == "ScalarType?": return f"c10::optional<at::ScalarType> {name}"
    if stype == "ScalarType":
      return f"at::ScalarType {name}"
    if stype == "AnyScalar":
      return f"at::Scalar {name}"
    if stype in builtin_types:
      return f"{builtin_types[stype][2]} {name}"
    raise ValueError(f"Problem: no such type: {stype}")
  lower_arg_i = 0
  def format_lower_arg(arg):
    nonlocal lower_arg_i
    name, stype, _ = arg
    if name == "shape":
      return "shape()"
    if stype == "Tensor":
      i = lower_arg_i
      lower_arg_i += 1
      return "loctx->GetOutputOp(operand(" + str(i) + "))"
    if stype == "[Tensor]":
      i = lower_arg_i
      lower_arg_i += 1
      return "GetArrayOperands(loctx, operands(), " + str(i) + ")"
    return f"{name}_"
  clone_arg_i = 0
  def format_clone_arg(arg):
    nonlocal clone_arg_i
    name, stype, _ = arg
    if name == "shape":
      return "shape()"
    if stype == "Tensor":
      i = clone_arg_i
      clone_arg_i += 1
      return "operands.at(" + str(i) + ")"
    if stype == "[Tensor]":
      i = clone_arg_i
      clone_arg_i += 1
      return "operands.subspan(" + str(i) + ")"
    return f"{name}_"
  def format_attr_define(arg):
    name, stype, _ = arg
    if name == "shape":
      return ""
    if stype == "Int64": return f"  xla::int64 {name}_;\n"
    if stype == "Bool": return f"  bool {name}_;\n"
    if stype == "Float": return "  float " + name + "_;\n"
    if stype == "ScalarType?": return (f"  c10::optional<at::ScalarType> "
                                       f"{name}_;\n")
    if stype == "ScalarType":
      return f"  at::ScalarType {name}_;\n"
    if stype == "AnyScalar":
      return f"  at::Scalar {name}_;"
    if stype == "[Int64]":
      return f"  std::vector<xla::int64> {name}_;\n"
    if stype in builtin_types:
      return f"  {builtin_types[stype][2]} {name}_;\n"
    raise ValueError(f"Problem: no such type: {stype}")
  def format_attr_init(arg):
    return f",\n        {arg[0]}_(std::move({arg[0]}))"

  shape_fn = None # f"""{{}}\n#error no shape function for {op["op_node_name"]}\n"""
  def resolve_shape_fn(shape_fn):
    for arg in tensor_args:
      if arg[0] == shape_fn: return f"{arg[0]}.shape()"
    if shape_fn == "shape":
      return "shape"
    return f"""{shape_fn}({", ".join(arg[0] for arg in op["args"])})"""
  def format_shape_lower_arg(arg):
    name, stype, _ = arg
    if stype == "Tensor": return f"{name}_ir"
    if stype == "[Tensor]":
      return f"{name}_ir"
    return name
  param_convert_i = 0
  def param_convert(arg):
    nonlocal param_convert_i
    i = param_convert_i
    param_convert_i += 1
    name, stype, _ = arg
    if stype == "[Tensor]":
      return f"       auto {name}_ir = MakeParameterList(&b, {i}, {name}, \"p{i}\");\n"
    else:
      return f"       auto {name}_ir = xla::Parameter(&b, {i}, {name}.shape(), \"p{i}\");\n"

  if "shape_fn" in op:
    shape_fn = resolve_shape_fn(op["shape_fn"])
  if shape_fn == None:
    if op["n_results"] == 1:
      shape_fn = f"""[&]() {{
       xla::XlaBuilder b("InferOutputShape");
{"".join(param_convert(arg) for arg in tensor_args)}       xla::XlaOp result = {op["lower_fn"]}(
         {", ".join(format_shape_lower_arg(arg) for arg in op["args"])});
       return XlaHelpers::ShapeOfXlaOp(result);
     }}"""
    else:
      shape_fn = f"""[&]() {{
       xla::XlaBuilder b("InferOutputShape");
{"".join(param_convert(arg) for arg in tensor_args)}       auto results = {op["lower_fn"]}(
         {", ".join(format_shape_lower_arg(arg) for arg in op["args"])});
       return ShapeOfXlaOpList(results);
     }}"""
  num_outputs = op["n_results"]
  ctx = []
  if "needs_lowering_context" in [i[0] for i in op["extras"]]:
    ctx = ["loctx"]
  tensors_ctor = f"""{{{", ".join(arg[0] for arg in tensor_args if arg[1] == "Tensor")}}}"""
  if has_tensor_list_arg:
    if len(tensor_args) == 1:
      tensors_ctor = tensor_args[-1][0]
    else:
      tensors_ctor = f"""TensorArgsConcat({tensors_ctor}, {tensor_args[-1][0]})"""
  lower_body = None
  if num_outputs == 1:
    lower_body = f"""
    xla::XlaOp result = {op["lower_fn"]}(
        {", ".join([format_lower_arg(arg) for arg in op["args"]] + ctx)});
    return ReturnOp(result, loctx);
  """
  else:
    lower_body = f"""
    auto result = {op["lower_fn"]}(
        {", ".join([format_lower_arg(arg) for arg in op["args"]] + ctx)});
    return ReturnOps(result, loctx);
  """

  return f"""
class {op["op_node_name"]} : public Node {{
 public:
  {op["op_node_name"]}({", ".join(format_ctor_arg(arg) for arg in op["args"])})
      : Node(ir::OpKind({op["x10_enum"]}),
             {tensors_ctor}, {shape_fn},
             /*num_outputs=*/{str(num_outputs)}, xla::util::MHash({", ".join(arg[0] for arg in attr_args)})){
"".join(format_attr_init(arg) for arg in attr_args if arg[0] != "shape")
} {{}}

  NodePtr Clone(OpList operands) const override {{
    return MakeNode<{op["op_node_name"]}>(
        {", ".join(format_clone_arg(arg) for arg in op["args"])});
  }}

  XlaOpVector Lower(LoweringContext* loctx) const override {{{lower_body}}}

  std::string ToString() const override {{
    std::stringstream ss;
    ss << Node::ToString();
{"".join(format_pretty_print(arg) for arg in attr_args)}    return ss.str();
  }}

 private:
{"".join(format_attr_define(arg) for arg in attr_args)}}};
"""

def c_function_define(op):
  args = op["args"]
  tensor_args = [
      arg for arg in args if arg[1] == "Tensor" or arg[1] == "[Tensor]"
  ]
  tensor_names = [arg[0] for arg in tensor_args]
  first_tensor = None
  if "result_dtype" in op and op["result_dtype"] in tensor_names:
    first_tensor = op["result_dtype"]
  if "shape_fn" in op and op["shape_fn"] in tensor_names:
    first_tensor = op["shape_fn"]
  if not first_tensor:
    if tensor_args[0][1] == "[Tensor]":
      first_tensor = f"swift_xla::FirstTensor({tensor_args[0][0]})"
    else:
      first_tensor = tensor_args[0][0]

  def format_arg_def(arg):
    name, stype, _ = arg
    if stype == "Tensor": return "OpaqueXLATensor* " + name
    if stype == "[Tensor]":
      return "OpaqueXLATensorArrayRef " + name
    if stype == "Int64": return "int64_t " + name
    if stype == "Float": return "float " + name
    if stype == "Bool": return f"bool {name}"
    if stype == "ScalarType?": return f"Optional_XLAScalarType {name}"
    if stype == "ScalarType":
      return f"XLATensorScalarType {name}"
    if stype == "AnyScalar":
      return f"XLAScalar {name}"
    if stype == "[Int64]":
      return f"Int64ArrayRef {name}"
    if stype in builtin_types:
      return f"{builtin_types[stype][0]} {name}"
    raise ValueError("problem unknown type: " + stype)
  def format_arg_ref(arg):
    name, stype, _ = arg
    if stype == "Tensor": return name + "_ir_value"
    if stype == "[Tensor]":
      return name + "_ir_value"
    if name == "shape":
      return ("swift_xla::MakeArrayShapeFromDimensions(shape.slice(), {}, " +
              f"{first_tensor}->shape().get().element_type(), "
              f"{first_tensor}->GetDevice().hw_type)")
    if stype in builtin_types:
      return builtin_types[stype][1](name)
    for extra in op["extras"]:
      if extra[0] == "canonicalize" and extra[1] == name:
        if stype == "[Int64]":
          if len(extra) == 4:
            return (f"swift_xla::ir::ops::{extra[3]}({extra[2]}_ir_value.shape(),"
                    f" {name}.slice())")
          else:
            return f"swift_xla::XlaHelpers::GetCanonicalDimensionIndices({name}.slice(), {extra[2]}_ir_value.shape().rank())"
        else:
          if len(extra) == 4:
            return (
                f"swift_xla::ir::ops::{extra[3]}({extra[2]}_ir_value, {name})")
          return f"swift_xla::XlaHelpers::GetCanonicalDimensionIndex({name}, {extra[2]}_ir_value.shape().rank())"
    if stype == "ScalarType?": return f"{name}.value()"
    if stype == "ScalarType":
      return f"ToScalarType({name})"
    if stype == "AnyScalar":
      return f"atScalar({name})"
    if stype == "[Int64]":
      return f"swift_xla::XlaHelpers::I64List({name}.slice())"
    return name
  def unpack_arg(arg):
    name, stype, _ = arg
    if stype == "Tensor": return f"  auto {name}_ir_value = {name}->GetIrValue();\n"
    if stype == "[Tensor]":
      return f"  auto {name}_ir_value = swift_xla::UnpackIrValues({name});\n"
    return ""
  node_ctor = f"""swift_xla::ir::MakeNode<swift_xla::ir::ops::{op["op_node_name"]}>({", ".join(format_arg_ref(arg) for arg in op["args"])})"""
  result_type = None
  if op["n_results"] == 1:
    result_type = "OpaqueXLATensor*"
  elif op["n_results"] == 2:
    result_type = "OpaqueXLATensor_pair"
  elif op["n_results"] == 3:
    result_type = "OpaqueXLATensor_tuple_3"
  else:
    raise ValueError(
        f"""{op["c_name"]} has unsupported number of return values {op["n_results"]}"""
    )

  def listify(l):
    if type(l) is list:
      return l
    return [l]

  dtypes = (([None] * op["n_results"])
            if "result_dtype" not in op else listify(op["result_dtype"]))

  def format_result(result_i=0, dtype=None):
    if not dtype:
      dtype = dtypes[result_i]
    if not dtype:
      return (f"new "
              f"swift_xla::XLATensor({first_tensor}->CreateFrom(swift_xla::ir::Value(result_node,"
              f" {result_i})))")
    if dtype in tensor_names:
      return f"new swift_xla::XLATensor({dtype}->CreateFrom(swift_xla::ir::Value(result_node, {result_i})))"
    result_dtype_arg = None
    for arg in args:
      if arg[0] == dtype:
        result_dtype_arg = arg
    if result_dtype_arg:
      return (f"new "
              f"swift_xla::XLATensor({first_tensor}->CreateFrom(swift_xla::ir::Value(result_node,"
              f" {result_i}), {format_arg_ref(result_dtype_arg)}))")
    return (f"new "
            f"swift_xla::XLATensor({first_tensor}->CreateFrom(swift_xla::ir::Value(result_node,"
            f" {result_i}), at::ScalarType::{dtype}))")

  prelude = f"""
{result_type} XLATensor_{op["c_name"]}({", ".join(format_arg_def(arg) for arg in op["args"])}) {{
{"".join(unpack_arg(arg) for arg in op["args"])}
  auto result_node = {node_ctor};"""
  if op["n_results"] != 1:
    tuple_names = []
    if op["n_results"] == 2:
      tuple_names = ["x", "y"]
    else:
      tuple_names = [f"v{i}" for i in range(op["n_results"])]
    out = f"""{prelude}
  {result_type} result;
"""
    for i in range(op["n_results"]):
      out += f"""  result.{tuple_names[i]} = {format_result(i)};
"""
    out += """  return result;
}
"""
    return out
  else:
    return f"""{prelude}
  return {format_result(0)};
}}
"""


def format_args(items, comma=", ", ending=""):
  result = ""
  for item in items:
    if result:
      result += comma
    result += item
  if result:
    return result + ending
  return ""


def swift_wrapper_define(op):
  args = op["args"]
  results = op["results"]

  def format_swift_arg(arg):
    name, stype, (is_explicit, full_stype) = arg
    if is_explicit:
      return f"{name}: {full_stype}"
    return f"_ {name}: {full_stype}"

  def format_result_type(r):
    return f"{r[0]} {r[1]}" if r[0] else r[1]

  def format_tuple_packing(ridx):
    r = results[ridx]
    tag = "xy"[ridx] if len(results) == 2 else f"v{ridx}"
    value = f"Tensor(_xlaHandle: tuple_output.{tag})"
    return f"{r[0]}: {value}" if r[0] else value

  generics = format_args((f"\n    {k}: {v}" for k, v in op["generics"].items()),
                         comma=",",
                         ending="\n  ")
  if generics:
    generics = f"<{generics}>"
  args_gen = format_args(("\n    " + format_swift_arg(arg) for arg in args),
                         comma=",",
                         ending="\n  ")
  results_gen = results[0][1] if len(
      results
  ) == 1 else f"({format_args((format_result_type(r) for r in results))})"

  def format_defer(arg):
    name, stype, (is_explicit, full_stype) = arg
    if stype == "Tensor":
      return f"\n    defer {{ _fixLifetime({name}) }}"
    return ""

  defers = format_args((format_defer(arg) for arg in args), comma="")

  def format_arg_ref(arg):
    name, stype, (is_explicit, full_stype) = arg
    if stype == "Tensor":
      return f"{name}.xlaHandle"
    if stype == "AnyScalar":
      return f"{name}.xlaScalar"
    return name

  body = ""
  last_tensor = None
  for arg in args:
    if arg[1] == "Tensor":
      if last_tensor:
        body += (f"    checkSameDevice({last_tensor[0]}.device, "
                 f"{arg[0]}.device)\n")
        if last_tensor[2][1] == arg[2][1]:
          body += f"    checkSamePrecision({last_tensor[0]}, {arg[0]})\n"
      else:
        last_tensor = arg
  withCounter = 0
  for arg in args:
    if arg[1][0] == "[":  # is array type.
      withCounter += 1
      body += f"""{"  " * withCounter}  return {arg[0]}.withArrayRef {{ {arg[0]} in\n"""
  dispatch = f"""XLATensor_{op["c_name"]}({format_args(format_arg_ref(arg) for arg in args)})"""
  if len(results) == 1:
    body += f"""{"  " * withCounter}    return Tensor(_xlaHandle: {dispatch})
"""
  else:
    body += f"""{"  " * withCounter}    let tuple_output = {dispatch}
"""
    body += f"""{"  " * withCounter}    return ({format_args(format_tuple_packing(ridx) for ridx in range(len(results)))})
"""

  for withCounter in range(withCounter, 0, -1):
    body += f"""{"  " * withCounter}  }}\n"""

  protection = "" if op[
      "protection"] == "internal" else f"""{op["protection"]} """
  return f"""
  {protection}static func {op["swift_name"]}{generics}({args_gen}) -> {results_gen} {{{defers}
{body}  }}
"""


def snake_to_camel(name):
  return "".join(map(lambda x: x[0].capitalize() + x[1:],name.split("_")))


def erase_generics(tname):
  if re.fullmatch("Tensor<(\w+)>", tname):
    return "Tensor"
  elif re.fullmatch("\[Tensor<(\w+)>\]", tname):
    return "[Tensor]"
  return tname


def canonicalize_op(op):
  tokens = re.findall("([\w\[\]<>]+\??|[\(\),:]|->)", op["def"])
  op["c_name"] = tokens[0]
  def expect(cond):
    if not cond: raise ValueError(f"""invalid format: {repr(op["def"])}""")
  expect(tokens[1] == '(')
  def isWord(idx):
    return re.match("[\w\[\]<>]+", tokens[idx]) != None

  i = 2
  args = []
  if tokens[i] != ')':
    while True:
      explicit_label = True
      if tokens[i] == "_":
        i += 1
        explicit_label = False
      expect(tokens[i + 1] == ':')
      expect(isWord(i) and isWord(i + 2))
      args.append((tokens[i], erase_generics(tokens[i + 2]), (explicit_label,
                                                              tokens[i + 2])))
      i += 3
      if tokens[i] == ')': break
      expect(tokens[i] == ',')
      i += 1
  i += 1
  expect(tokens[i] == "->")
  i += 1
  n_results = 0
  results = []
  if tokens[i] == "(":
    i += 1
    while True:
      name = ""
      if tokens[i + 1] == ":":
        name = tokens[i]
        i += 2
      expect(erase_generics(tokens[i]) == "Tensor")
      results.append(("", tokens[i]))
      n_results += 1
      i += 1
      if tokens[i] == ")":
        break
      expect(tokens[i] == ",")
      i += 1
  else:
    expect(erase_generics(tokens[i]) == "Tensor")
    results.append(("", tokens[i]))
    n_results = 1
  i += 1

  op["n_results"] = n_results
  op["results"] = results
  if "x10_enum" in op:
    if op["x10_enum"] == f"""at::aten::{op["c_name"]}""":
      print(f"Extranious enum: {op['x10_enum']}")
  else:
    op["x10_enum"] = f"""at::aten::{op["c_name"]}"""
  op["args"] = args
  if "op_node_name" not in op: op["op_node_name"] = snake_to_camel(op["c_name"])
  if "swift_name" not in op:
    op["swift_name"] = op["c_name"]
  if "extras" in op:
    op["extras"] = [a.split() for a in op["extras"]]
  else:
    op["extras"] = []
  # If there are type annotations, ensure swift wrapper generation.
  is_swift = not ([erase_generics(r[1]) for r in results
                  ] == [r[1] for r in results])
  if is_swift and "swift_namespace" not in op:
    op["swift_namespace"] = "_RawXLA"
  if is_swift and "generics" not in op:
    op["generics"] = {}
  if "protection" not in op:
    op["protection"] = "public"

  del op["def"]

def main(argv):
  if len(argv) > 1:
    raise app.UsageError("Too many command-line arguments.")
  op_list = yaml.full_load(open(FLAGS.def_file).read())
  for op in op_list: canonicalize_op(op)

  name_order = [op["op_node_name"] for op in op_list]
  name_order_sorted = name_order[:]
  name_order_sorted.sort()
  if name_order != name_order_sorted:
    for i in range(len(name_order)):
      if name_order[i] != name_order_sorted[i]:
        print(f"{name_order[i]} -> {name_order_sorted[i]}")
    raise ValueError("op list is not sorted")
  print(f"\n\n\n{len(name_order)} total ops\n\n\n")

  open(FLAGS.cc_output, "w+").write(HEADER + """
namespace swift_xla {
namespace ir {
namespace ops {
namespace {
""" + ("".join(node_type_define(op) for op in op_list)) + """
}  // namespace
}  // namespace ops
}  // namespace ir
}  // namespace swift_xla
""" + "".join(c_function_define(op) for op in op_list))

  open(FLAGS.swift_output, "w+").write(HEADER + """
@_implementationOnly import x10_xla_tensor_tf_ops
@_implementationOnly import x10_xla_tensor_wrapper

extension _RawXLA {
""" + ("".join(
    swift_wrapper_define(op)
    for op in op_list
    if "swift_namespace" in op and op["swift_namespace"] == "_RawXLA")) + """
}
""")
  for op in op_list:
    if not ("swift_namespace" in op and op["swift_namespace"] == "_RawXLA"):
      print(f"""Missing swift types: {op["op_node_name"]}""")


if __name__ == "__main__":
  app.run(main)

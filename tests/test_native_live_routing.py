"""Observable graph-routing decisions for normal and unusual contexts."""
import unittest

from docxtpl_native import Listing, RichText, Subdoc
from docxtpl_native.bridge import requires_live_model


class NativeLiveRoutingTests(unittest.TestCase):
    def test_metaclass_equality_does_not_make_an_object_a_scalar(self) -> None:
        events: list[object] = []

        class ScalarMeta(type):
            def __eq__(cls, other: object) -> bool:
                events.append(other)
                return other is int

            __hash__ = type.__hash__

        class Value(metaclass=ScalarMeta):
            pass

        self.assertTrue(requires_live_model({'value': Value()}, None))
        self.assertEqual(events, [])

    def test_ordinary_nested_values_and_text(self):
        context={'plain':[None,True,3,1.5,b'bytes',('safe','\t\n\r','\ud800')], 'rich':RichText('text'), 'listing':Listing('text')}
        self.assertFalse(requires_live_model(context,None))
        self.assertTrue(requires_live_model(context,False))
        for text in ('<','&','\x00','\x1f','prefix\x0bsuffix'):
            with self.subTest(text=repr(text)):self.assertTrue(requires_live_model({'value':text},None))

    def test_cycles_and_exact_container_types(self):
        cycle=[];mapping={'cycle':cycle};cycle.extend([mapping,cycle])
        self.assertFalse(requires_live_model(mapping,None))
        mapping['unsafe']=['text & more']
        self.assertTrue(requires_live_model(mapping,None))
        class DictSubclass(dict):pass
        class ListSubclass(list):pass
        class StringSubclass(str):pass
        for value in (DictSubclass(),ListSubclass(),StringSubclass('safe')):
            with self.subTest(kind=type(value).__name__):self.assertTrue(requires_live_model(value,None))
        self.assertTrue(requires_live_model({1:'value'},None))

    def test_subdocument_disables_live_graph_only_when_traversed(self):
        subdoc=object.__new__(Subdoc)
        self.assertFalse(requires_live_model({'subdoc':subdoc,'unsafe':'<'},object()))
        class ListSubclass(list):pass
        self.assertTrue(requires_live_model(ListSubclass([subdoc]),None))

    def test_opaque_objects_are_not_inspected(self):
        class Opaque:
            def __getattribute__(self,name):raise AssertionError(name)
            def __iter__(self):raise AssertionError('iter')
            def __hash__(self):raise AssertionError('hash')
            def __bool__(self):raise AssertionError('bool')
        value=Opaque()
        self.assertTrue(requires_live_model({'item':value},None))

    def test_seeded_nested_contexts_with_known_outcomes(self):
        import random
        rng=random.Random(20261104);subdoc=object.__new__(Subdoc)
        class Opaque:pass
        class ListSubclass(list):pass
        leaves=[(None,False,False),(1,False,False),('safe',False,False),('\ud800',False,False),
                ('<',True,False),('&',True,False),('\x00',True,False),(Opaque(),True,False),
                (ListSubclass(),True,False),(subdoc,False,True)]
        def make(depth):
            if depth==0 or rng.randrange(4)==0:return rng.choice(leaves)
            items=[make(depth-1) for _ in range(rng.randrange(5))]
            observable=any(item[1] for item in items);native_files=any(item[2] for item in items)
            if rng.randrange(2):return [item[0] for item in items],observable,native_files
            context={}
            for index,item in enumerate(items):
                key=index if rng.randrange(2) else str(index)
                if type(key) is not str:observable=True
                context[key]=item[0]
            return context,observable,native_files
        for index in range(2000):
            context,observable,native_files=make(4)
            if index%17==0:
                cycle=[context];cycle.append(cycle);context=cycle
            environment=None if index%3 else object()
            expected=(environment is not None or observable) and not native_files
            with self.subTest(index=index):self.assertEqual(requires_live_model(context,environment),expected)


if __name__=='__main__':unittest.main()
